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


def test_risk_state_effective_capital_roundtrip():
    repo = TradeRepository(":memory:")
    repo.save_risk_state(RiskState(daily_date="2026-01-01", effective_capital_usdt=345.67))
    loaded = repo.get_risk_state()
    assert loaded.effective_capital_usdt == 345.67


def test_get_total_realized_pnl_usdt_sums_only_closed_trades():
    repo = TradeRepository(":memory:")
    assert repo.get_total_realized_pnl_usdt() == 0.0

    id1 = repo.create_trade(Trade(symbol="BTCUSDT"))
    repo.update_trade(id1, status=STATUS_CLOSED, pnl_usdt=12.5)
    id2 = repo.create_trade(Trade(symbol="ETHUSDT"))
    repo.update_trade(id2, status=STATUS_CLOSED, pnl_usdt=-4.25)
    # Not closed -- must NOT be counted even though it happens to carry a pnl value.
    id3 = repo.create_trade(Trade(symbol="BNBUSDT"))
    repo.update_trade(id3, status=STATUS_OPEN, pnl_usdt=999.0)

    assert repo.get_total_realized_pnl_usdt() == 8.25


def test_migration_adds_effective_capital_column_to_pre_existing_db(tmp_path):
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    # Simulate a database created by an older version of the bot, before
    # effective_capital_usdt existed.
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.executescript(
        """
        CREATE TABLE risk_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            daily_date TEXT NOT NULL,
            daily_realized_pnl_usdt REAL NOT NULL DEFAULT 0,
            daily_stopped INTEGER NOT NULL DEFAULT 0,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            pause_until TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    legacy_conn.execute(
        "INSERT INTO risk_state (id, daily_date, updated_at) VALUES (1, '2026-01-01', '2026-01-01T00:00:00+00:00')"
    )
    legacy_conn.commit()
    legacy_conn.close()

    # Opening through TradeRepository must migrate the schema without losing data.
    repo = TradeRepository(db_path)
    state = repo.get_risk_state()
    assert state is not None
    assert state.daily_date == "2026-01-01"
    assert state.effective_capital_usdt == 0.0

    repo.save_risk_state(RiskState(daily_date="2026-01-01", effective_capital_usdt=500.0))
    assert repo.get_risk_state().effective_capital_usdt == 500.0
