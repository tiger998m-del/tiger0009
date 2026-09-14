from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.recovery import run_recovery
from app.database.models import STATUS_NEEDS_MANUAL_REVIEW, STATUS_OPEN, STATUS_REJECTED, Trade
from app.database.repository import TradeRepository
from app.exchange.symbol_filters import SymbolFilters
from app.execution.order_executor import OrderExecutor
from app.execution.position_manager import PositionManager
from app.notifications.telegram_notifier import TelegramNotifier
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine
from tests.conftest import make_settings

SYMBOL = "BTCUSDT"


def _filters() -> SymbolFilters:
    return SymbolFilters(
        symbol=SYMBOL, base_asset="BTC", quote_asset="USDT", status="TRADING",
        is_spot_trading_allowed=True, base_asset_precision=8, quote_asset_precision=8,
        tick_size=Decimal("0.01"), min_price=Decimal("0.01"), max_price=Decimal("1000000"),
        step_size=Decimal("0.00001"), min_qty=Decimal("0.00001"), max_qty=Decimal("9000"),
        market_step_size=Decimal("0.00001"), market_min_qty=Decimal("0.00001"), market_max_qty=Decimal("3200"),
        min_notional=Decimal("10"), apply_min_notional_to_market=True,
    )


def _setup(dry_run: bool):
    settings = make_settings(
        dry_run=dry_run,
        binance_api_key="test-key" if not dry_run else "",
        binance_api_secret="test-secret" if not dry_run else "",
    )
    repo = TradeRepository(":memory:")
    risk = RiskManager(settings, repo)
    executor = OrderExecutor(rest=AsyncMock(), dry_run=dry_run, quote_asset="USDT")
    notifier = TelegramNotifier("", "", "@x")
    notifier.send = AsyncMock()
    engine = SignalEngine(settings)
    filters_map = {SYMBOL: _filters()}
    pm = PositionManager(settings, repo, risk, executor, rest=AsyncMock(), notifier=notifier,
                          signal_engine=engine, filters_map=filters_map)
    return settings, repo, risk, notifier, pm, filters_map


@pytest.mark.asyncio
async def test_recovery_marks_never_filled_pending_entry_as_rejected():
    settings, repo, risk, notifier, pm, filters_map = _setup(dry_run=False)
    trade_id = repo.create_trade(Trade(symbol=SYMBOL))
    repo.update_trade(trade_id, buy_order_id=123)

    rest = AsyncMock()
    rest.get_order = AsyncMock(return_value={"status": "CANCELED"})

    report = await run_recovery(settings, rest, repo, pm, risk, notifier, filters_map)

    trade = repo.get_trade(trade_id)
    assert trade.status == STATUS_REJECTED
    assert report.restored == 0
    assert report.mismatches == 0


@pytest.mark.asyncio
async def test_recovery_restores_consistent_open_position():
    settings, repo, risk, notifier, pm, filters_map = _setup(dry_run=False)
    trade_id = repo.create_trade(Trade(symbol=SYMBOL))
    repo.update_trade(
        trade_id, status=STATUS_OPEN, buy_order_id=1, tp_order_id=2,
        avg_entry_price=100.0, executed_qty=1.0, tp_price=103.0, active_atr_stop=98.0, best_stop=98.0,
    )

    rest = AsyncMock()
    rest.get_order = AsyncMock(return_value={"status": "NEW"})  # TP order still open
    rest.account_info = AsyncMock(return_value={"balances": [{"asset": "BTC", "free": "1.0", "locked": "0.0"}]})

    report = await run_recovery(settings, rest, repo, pm, risk, notifier, filters_map)

    assert report.restored == 1
    assert report.mismatches == 0
    assert SYMBOL in pm.open_symbols()

    # Clean up the background TP-poll task the restored position spawned.
    position = pm._open[SYMBOL]
    if position.tp_poll_task:
        position.tp_poll_task.cancel()


@pytest.mark.asyncio
async def test_recovery_flags_mismatch_when_coins_not_actually_held():
    settings, repo, risk, notifier, pm, filters_map = _setup(dry_run=False)
    trade_id = repo.create_trade(Trade(symbol=SYMBOL))
    repo.update_trade(
        trade_id, status=STATUS_OPEN, buy_order_id=1, tp_order_id=2,
        avg_entry_price=100.0, executed_qty=1.0, tp_price=103.0, active_atr_stop=98.0, best_stop=98.0,
    )

    rest = AsyncMock()
    rest.get_order = AsyncMock(return_value={"status": "NEW"})
    # Binance shows we hold NONE of the coin -- serious mismatch, must not guess.
    rest.account_info = AsyncMock(return_value={"balances": [{"asset": "BTC", "free": "0.0", "locked": "0.0"}]})

    report = await run_recovery(settings, rest, repo, pm, risk, notifier, filters_map)

    trade = repo.get_trade(trade_id)
    assert trade.status == STATUS_NEEDS_MANUAL_REVIEW
    assert report.mismatches == 1
    assert SYMBOL not in pm.open_symbols()
    notifier.send.assert_awaited()


@pytest.mark.asyncio
async def test_recovery_closes_trade_filled_by_tp_while_offline():
    settings, repo, risk, notifier, pm, filters_map = _setup(dry_run=False)
    trade_id = repo.create_trade(Trade(symbol=SYMBOL))
    repo.update_trade(
        trade_id, status=STATUS_OPEN, buy_order_id=1, tp_order_id=2,
        avg_entry_price=100.0, executed_qty=1.0, tp_price=103.0, active_atr_stop=98.0, best_stop=98.0,
    )

    rest = AsyncMock()
    rest.get_order = AsyncMock(return_value={
        "status": "FILLED", "executedQty": "1.0", "cummulativeQuoteQty": "103.0",
    })
    rest.account_info = AsyncMock(return_value={"balances": [{"asset": "BTC", "free": "0.0", "locked": "0.0"}]})

    report = await run_recovery(settings, rest, repo, pm, risk, notifier, filters_map)

    trade = repo.get_trade(trade_id)
    assert trade.status == "CLOSED"
    assert trade.exit_reason == "TAKE_PROFIT_DURING_DOWNTIME"
    assert trade.pnl_usdt == pytest.approx(3.0)
    assert report.restored == 0
