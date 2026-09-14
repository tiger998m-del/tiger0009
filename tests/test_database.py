from __future__ import annotations

from app.database.models import STATUS_CLOSED, STATUS_OPEN, RiskState, Trade
from app.database.repository import TradeRepository


def test_create_and_fetch_trade_roundtrip():
    repo = TradeRepository(":memory:")
    trade_id = repo.create_trade(Trade(symbol="BTCUSDT", capital_used_usdt=128.0))
    trade = repo.get_trade(trade_id)
    assert trade is not None
    assert trade.symbol == "BTCUSDT"
    assert trade.capital_used_usdt == 128.0
    assert trade.status == "PENDING_ENTRY"


def test_update_trade_persists_all_fields():
    repo = TradeRepository(":memory:")
    trade_id = repo.create_trade(Trade(symbol="ETHUSDT"))
    repo.update_trade(
        trade_id, status=STATUS_OPEN, avg_entry_price=1800.5, executed_qty=0.5,
        tp_price=1854.5, active_atr_stop=1750.0, best_stop=1750.0, buy_order_id=555,
    )
    trade = repo.get_trade(trade_id)
    assert trade.status == STATUS_OPEN
    assert trade.avg_entry_price == 1800.5
    assert trade.executed_qty == 0.5
    assert trade.tp_price == 1854.5
    assert trade.buy_order_id == 555


def test_get_open_trades_excludes_closed_and_rejected():
    repo = TradeRepository(":memory:")
    open_id = repo.create_trade(Trade(symbol="BTCUSDT"))
    repo.update_trade(open_id, status=STATUS_OPEN)
    closed_id = repo.create_trade(Trade(symbol="ETHUSDT"))
    repo.update_trade(closed_id, status=STATUS_CLOSED)
    rejected_id = repo.create_trade(Trade(symbol="BNBUSDT"))
    repo.update_trade(rejected_id, status="REJECTED")

    open_trades = repo.get_open_trades()
    symbols = {t.symbol for t in open_trades}
    assert symbols == {"BTCUSDT"}


def test_get_open_trade_for_symbol():
    repo = TradeRepository(":memory:")
    trade_id = repo.create_trade(Trade(symbol="BTCUSDT"))
    repo.update_trade(trade_id, status=STATUS_OPEN)
    found = repo.get_open_trade_for_symbol("BTCUSDT")
    assert found is not None
    assert found.id == trade_id
    assert repo.get_open_trade_for_symbol("ETHUSDT") is None


def test_risk_state_persistence_roundtrip():
    repo = TradeRepository(":memory:")
    assert repo.get_risk_state() is None

    state = RiskState(daily_date="2026-01-01", daily_realized_pnl_usdt=-5.5,
                       daily_stopped=True, consecutive_losses=2, pause_until="2026-01-01T12:00:00+00:00")
    repo.save_risk_state(state)

    loaded = repo.get_risk_state()
    assert loaded.daily_date == "2026-01-01"
    assert loaded.daily_realized_pnl_usdt == -5.5
    assert loaded.daily_stopped is True
    assert loaded.consecutive_losses == 2
    assert loaded.pause_until == "2026-01-01T12:00:00+00:00"


def test_risk_state_upsert_overwrites_previous_value():
    repo = TradeRepository(":memory:")
    repo.save_risk_state(RiskState(daily_date="2026-01-01", daily_realized_pnl_usdt=-1.0))
    repo.save_risk_state(RiskState(daily_date="2026-01-01", daily_realized_pnl_usdt=-9.0))
    loaded = repo.get_risk_state()
    assert loaded.daily_realized_pnl_usdt == -9.0
