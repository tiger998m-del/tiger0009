from __future__ import annotations

from app.database.models import STATUS_CLOSED, Trade
from app.database.repository import TradeRepository
from app.risk.risk_manager import RiskManager
from app.utils.time_utils import utc_date_str
from tests.conftest import make_settings


def _close_trade(repo: TradeRepository, pnl_usdt: float) -> None:
    trade_id = repo.create_trade(Trade(symbol="BTCUSDT"))
    repo.update_trade(trade_id, status=STATUS_CLOSED, pnl_usdt=pnl_usdt)


def test_reinvestment_disabled_keeps_capital_fixed_regardless_of_pnl():
    settings = make_settings(reinvest_profits=False, bot_capital_sar=1000.0, sar_per_usdt=3.75)
    repo = TradeRepository(":memory:")
    _close_trade(repo, pnl_usdt=+100.0)

    rm = RiskManager(settings, repo)
    assert rm.capital_usdt == settings.capital_usdt

    # Even after a "day rollover", capital stays at the fixed baseline.
    rm._state.daily_date = "2000-01-01"
    assert rm.capital_usdt == settings.capital_usdt


def test_reinvestment_enabled_reflects_cumulative_pnl_on_fresh_init():
    settings = make_settings(reinvest_profits=True, bot_capital_sar=1000.0, sar_per_usdt=3.75)
    repo = TradeRepository(":memory:")
    _close_trade(repo, pnl_usdt=+50.0)
    _close_trade(repo, pnl_usdt=-10.0)

    # A fresh RiskManager (e.g. right after a bot restart) always re-derives
    # the snapshot from the closed-trades ground truth.
    rm = RiskManager(settings, repo)
    assert rm.capital_usdt == settings.capital_usdt + 40.0


def test_reinvestment_is_frozen_within_the_same_trading_day_then_updates_on_restart(monkeypatch):
    fake_today = ["2026-01-01"]
    monkeypatch.setattr("app.risk.risk_manager.utc_date_str", lambda: fake_today[0])

    settings = make_settings(reinvest_profits=True, bot_capital_sar=1000.0, sar_per_usdt=3.75)
    repo = TradeRepository(":memory:")

    rm = RiskManager(settings, repo)
    base = rm.capital_usdt
    assert base == settings.capital_usdt  # no trades closed yet

    # A trade closes mid-day: the real (Binance-verified) PnL is persisted to
    # the trades table, and register_closed_trade() updates daily bookkeeping.
    _close_trade(repo, pnl_usdt=+75.0)
    rm.register_closed_trade(+75.0)

    # Capital used for sizing must NOT change mid-day (still "2026-01-01")...
    assert rm.capital_usdt == base

    # ...but a fresh instance (simulating a restart) picks it up immediately,
    # proving the PnL was correctly persisted even though today's snapshot
    # stayed frozen.
    rm2 = RiskManager(settings, repo)
    assert rm2.capital_usdt == base + 75.0


def test_reinvestment_recompounds_on_day_rollover(monkeypatch):
    fake_today = ["2026-01-01"]
    monkeypatch.setattr("app.risk.risk_manager.utc_date_str", lambda: fake_today[0])

    settings = make_settings(reinvest_profits=True, bot_capital_sar=1000.0, sar_per_usdt=3.75)
    repo = TradeRepository(":memory:")
    rm = RiskManager(settings, repo)
    base = rm.capital_usdt

    _close_trade(repo, pnl_usdt=+75.0)
    rm.register_closed_trade(+75.0)
    assert rm.capital_usdt == base  # frozen today

    # Advance to the next UTC day.
    fake_today[0] = "2026-01-02"
    assert rm.capital_usdt == base + 75.0  # re-snapshotted for the new day


def test_reinvestment_floors_effective_capital_at_zero():
    settings = make_settings(reinvest_profits=True, bot_capital_sar=1000.0, sar_per_usdt=3.75)
    repo = TradeRepository(":memory:")
    base = settings.capital_usdt
    _close_trade(repo, pnl_usdt=-(base * 5))  # catastrophic cumulative loss

    rm = RiskManager(settings, repo)
    assert rm.capital_usdt == 0.0
    assert rm.per_trade_usdt == 0.0

    # Fail-closed: sizing a trade against a real balance still yields zero,
    # never a negative amount.
    capital, reduced = rm.compute_trade_capital(available_balance_usdt=10_000.0)
    assert capital == 0.0


def test_per_trade_usdt_scales_with_compounded_capital():
    settings = make_settings(reinvest_profits=True, bot_capital_sar=1000.0, sar_per_usdt=3.75,
                              trade_allocation_pct=0.48)
    repo = TradeRepository(":memory:")
    _close_trade(repo, pnl_usdt=+100.0)

    rm = RiskManager(settings, repo)
    expected_capital = settings.capital_usdt + 100.0
    assert rm.per_trade_usdt == expected_capital * 0.48


def test_daily_stop_threshold_uses_compounded_capital():
    settings = make_settings(reinvest_profits=True, bot_capital_sar=1000.0, sar_per_usdt=3.75,
                              daily_stop_pct=0.02)
    repo = TradeRepository(":memory:")
    # Large prior profit roughly doubles the effective capital.
    _close_trade(repo, pnl_usdt=settings.capital_usdt)

    rm = RiskManager(settings, repo)
    compounded_capital = rm.capital_usdt
    threshold = -0.02 * compounded_capital

    # A loss that would have tripped Daily Stop against the ORIGINAL base
    # capital must NOT trip it now that capital has compounded higher.
    base_only_threshold = -0.02 * settings.capital_usdt
    loss_between_thresholds = base_only_threshold - 0.01  # just past the old threshold
    assert loss_between_thresholds > threshold  # confirm the compounded threshold is indeed more negative

    events = rm.register_closed_trade(loss_between_thresholds)
    assert not any(e.kind == "DAILY_STOP" for e in events)
    assert rm.is_daily_stopped is False


def test_check_capital_snapshot_event_fires_once_per_day_change(monkeypatch):
    fake_today = ["2026-01-01"]
    monkeypatch.setattr("app.risk.risk_manager.utc_date_str", lambda: fake_today[0])

    settings = make_settings(reinvest_profits=True)
    repo = TradeRepository(":memory:")
    rm = RiskManager(settings, repo)

    # No new day yet -- no event.
    assert rm.check_capital_snapshot_event() is None
    assert rm.check_capital_snapshot_event() is None

    fake_today[0] = "2026-01-02"
    event = rm.check_capital_snapshot_event()
    assert event is not None
    assert event.kind == "CAPITAL_SNAPSHOT"

    # Immediately calling again on the same (new) day must not re-fire.
    assert rm.check_capital_snapshot_event() is None
