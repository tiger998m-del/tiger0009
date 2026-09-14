from __future__ import annotations

from datetime import timedelta

from app.database.repository import TradeRepository
from app.risk.risk_manager import RiskManager
from app.utils.time_utils import utcnow
from tests.conftest import make_settings


def _rm(**overrides) -> RiskManager:
    settings = make_settings(**overrides)
    repo = TradeRepository(":memory:")
    return RiskManager(settings, repo)


def test_daily_stop_triggers_after_reaching_negative_2_percent():
    rm = _rm(bot_capital_sar=1000.0, sar_per_usdt=3.75, daily_stop_pct=0.02)
    capital_usdt = 1000.0 / 3.75
    threshold = -0.02 * capital_usdt

    assert rm.is_daily_stopped is False
    events = rm.register_closed_trade(threshold - 0.01)  # push just past the threshold
    kinds = [e.kind for e in events]
    assert "DAILY_STOP" in kinds
    assert rm.is_daily_stopped is True


def test_daily_stop_blocks_new_trades():
    rm = _rm(bot_capital_sar=1000.0, sar_per_usdt=3.75, daily_stop_pct=0.02)
    capital_usdt = 1000.0 / 3.75
    rm.register_closed_trade(-0.03 * capital_usdt)
    allowed, reason = rm.can_open_new_trade(current_open_count=0)
    assert allowed is False
    assert "Daily Stop" in reason


def test_daily_stop_not_triggered_by_small_loss():
    rm = _rm(bot_capital_sar=1000.0, sar_per_usdt=3.75, daily_stop_pct=0.02)
    capital_usdt = 1000.0 / 3.75
    events = rm.register_closed_trade(-0.005 * capital_usdt)
    assert not any(e.kind == "DAILY_STOP" for e in events)
    assert rm.is_daily_stopped is False


def test_consecutive_losses_trigger_pause():
    rm = _rm(consecutive_loss_limit=2, pause_minutes=60)
    rm.register_closed_trade(-1.0)
    assert rm.is_paused is False
    events = rm.register_closed_trade(-1.0)
    assert any(e.kind == "PAUSE_TRIGGERED" for e in events)
    assert rm.is_paused is True


def test_pause_blocks_new_trades_but_a_winning_trade_resets_the_streak():
    rm = _rm(consecutive_loss_limit=2, pause_minutes=60)
    rm.register_closed_trade(-1.0)
    rm.register_closed_trade(2.0)  # win resets consecutive loss counter
    events = rm.register_closed_trade(-1.0)
    assert not any(e.kind == "PAUSE_TRIGGERED" for e in events)
    allowed, _ = rm.can_open_new_trade(current_open_count=0)
    assert allowed is True


def test_pause_auto_resumes_after_window_expires():
    rm = _rm(consecutive_loss_limit=2, pause_minutes=60)
    rm.register_closed_trade(-1.0)
    rm.register_closed_trade(-1.0)
    assert rm.is_paused is True

    # Simulate time passing by forcing the internal pause_until into the past.
    rm._state.pause_until = (utcnow() - timedelta(minutes=1)).isoformat()
    assert rm.is_paused is False
    event = rm.check_pause_expired_event()
    assert event is not None
    assert event.kind == "PAUSE_ENDED"

    allowed, _ = rm.can_open_new_trade(current_open_count=0)
    assert allowed is True


def test_pause_does_not_affect_open_position_monitoring():
    # The pause flag only gates can_open_new_trade(); it must never be
    # consulted by exit-monitoring code paths (ATR stop / EMA8 exit / TP),
    # which operate directly on open positions regardless of pause state.
    rm = _rm(consecutive_loss_limit=1, pause_minutes=60)
    rm.register_closed_trade(-1.0)
    assert rm.is_paused is True
    # can_open_new_trade correctly blocks new entries...
    allowed, _ = rm.can_open_new_trade(current_open_count=0)
    assert allowed is False
    # ...but nothing in RiskManager exposes a way to block monitoring of an
    # already-open position -- that logic lives entirely in PositionManager
    # and is untouched by is_paused/is_daily_stopped.
