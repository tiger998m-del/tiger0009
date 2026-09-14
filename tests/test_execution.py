from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.database.repository import TradeRepository
from app.exchange.symbol_filters import SymbolFilters
from app.execution.order_executor import OrderExecutor, OrderFillResult
from app.execution.position_manager import PositionManager
from app.notifications.telegram_notifier import TelegramNotifier
from app.risk.risk_manager import RiskManager
from app.strategy.entry_signal import EntrySignalResult
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


def _silent_notifier() -> TelegramNotifier:
    notifier = TelegramNotifier("", "", "@x")
    notifier.send = AsyncMock()
    return notifier


def _build_pm(dry_run=True, order_executor=None):
    settings = make_settings(dry_run=dry_run)
    repo = TradeRepository(":memory:")
    risk = RiskManager(settings, repo)
    executor = order_executor or OrderExecutor(rest=AsyncMock(), dry_run=dry_run, quote_asset="USDT",
                                                poll_interval_seconds=0.01, poll_timeout_seconds=0.5)
    notifier = _silent_notifier()
    engine = SignalEngine(settings)
    filters_map = {SYMBOL: _filters()}
    pm = PositionManager(settings, repo, risk, executor, rest=AsyncMock(), notifier=notifier,
                          signal_engine=engine, filters_map=filters_map)
    return pm, repo, risk, executor


def _entry_result(close=100.0) -> EntrySignalResult:
    return EntrySignalResult(passed=True, reasons_failed=[], close=close,
                              initial_sl=close * 0.98, initial_sl_pct=2.0, risk_reward=1.5, atr14=1.0)


@pytest.mark.asyncio
async def test_duplicate_entry_is_prevented_when_position_already_open():
    pm, repo, risk, executor = _build_pm()
    ok1 = await pm.try_enter(SYMBOL, _entry_result())
    assert ok1 is True
    assert pm.open_count() == 1

    ok2 = await pm.try_enter(SYMBOL, _entry_result())
    assert ok2 is False
    assert pm.open_count() == 1  # still only one position, no second BUY happened


@pytest.mark.asyncio
async def test_duplicate_entry_is_prevented_under_concurrency():
    pm, repo, risk, executor = _build_pm()
    results = await asyncio.gather(
        pm.try_enter(SYMBOL, _entry_result()),
        pm.try_enter(SYMBOL, _entry_result()),
    )
    assert sorted(results) == [False, True]
    assert pm.open_count() == 1
    # Exactly one trade row should exist in the database.
    all_trades = repo.get_all_trades()
    assert len(all_trades) == 1


@pytest.mark.asyncio
async def test_double_sell_is_prevented_under_concurrent_exit_triggers():
    pm, repo, risk, executor = _build_pm()
    await pm.try_enter(SYMBOL, _entry_result())
    assert pm.open_count() == 1

    market_sell_calls = []

    async def slow_market_sell(symbol, qty, reference_price, filters):
        await asyncio.sleep(0.05)
        market_sell_calls.append((symbol, qty))
        return OrderFillResult(order_id=1, status="FILLED", avg_price=95.0,
                                executed_qty=float(qty), cumulative_quote_qty=float(qty) * 95.0, fees_usdt=0.0)

    executor.market_sell = slow_market_sell
    executor.cancel_order_safe = AsyncMock(return_value={"status": "CANCELED"})

    await asyncio.gather(
        pm.request_exit(SYMBOL, "ATR_STOP"),
        pm.request_exit(SYMBOL, "EMA8_EXIT"),
    )

    assert len(market_sell_calls) == 1  # only ONE sell actually executed
    assert pm.open_count() == 0
    closed = repo.get_recent_closed_trades()
    assert len(closed) == 1
    assert closed[0].status == "CLOSED"


@pytest.mark.asyncio
async def test_tp_fill_race_defers_to_take_profit_and_skips_market_sell():
    pm, repo, risk, executor = _build_pm()
    await pm.try_enter(SYMBOL, _entry_result())

    # Simulate: our ATR-stop trigger tries to cancel the TP order, but it
    # already filled on Binance's side moments earlier.
    executor.cancel_order_safe = AsyncMock(return_value={
        "status": "FILLED", "executedQty": "1.0", "cummulativeQuoteQty": "103.0",
    })
    executor.market_sell = AsyncMock()

    await pm.request_exit(SYMBOL, "ATR_STOP", trigger_price=Decimal("95"))

    executor.market_sell.assert_not_called()
    closed = repo.get_recent_closed_trades()
    assert closed[0].exit_reason == "TAKE_PROFIT"
