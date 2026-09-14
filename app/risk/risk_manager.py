"""
Risk Manager -- deliberately separated from strategy/signal logic.

Owns:
  - capital allocation & position sizing (48% per trade, from Settings/.env)
  - max concurrent open positions (2)
  - Daily Stop (-2% of bot capital / UTC day)
  - consecutive-loss pause (2 losses in a row -> 60 minute pause on NEW
    entries only; open positions keep being monitored/protected)
  - daily profit reinvestment / capital compounding (REINVEST_PROFITS)

State (daily pnl, daily-stopped flag, consecutive losses, pause_until,
effective/compounded capital) is persisted to SQLite through TradeRepository
so a bot restart does not reset protections that were already triggered --
this is essential for capital protection across crashes/restarts
(constitution #20/#22).

Reinvestment (REINVEST_PROFITS=true, the default): once per UTC trading day,
the capital actually used for position sizing is snapshotted as:

    effective_capital_usdt = MAX(base_capital_usdt + total_realized_pnl_usdt, 0)

where base_capital_usdt is the fixed BOT_CAPITAL_SAR/SAR_PER_USDT value from
.env, and total_realized_pnl_usdt is the sum of pnl_usdt across every CLOSED
trade ever recorded (the SQLite trades table is the ground truth, so this can
never drift). That snapshot is frozen for the rest of the trading day -- a
trade closing at 10:00 does not change the size of a trade opened at 11:00
the same day; the change only takes effect at the next UTC day boundary (or
on a restart, which always re-derives the snapshot from ground truth, so a
restart is never unsafe). This mirrors how Daily Stop already uses a frozen
daily reference point. If cumulative losses would take effective capital to
zero or below, it is floored at 0 -- position sizing then naturally computes
a per-trade size of 0 and no new trade can open, which is a safe fail-closed
outcome rather than a crash or a negative-size order.

Set REINVEST_PROFITS=false in .env to disable compounding entirely and
always size trades off the fixed baseline capital instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from app.config.settings import Settings
from app.database.models import RiskState
from app.database.repository import TradeRepository
from app.utils.logging_setup import get_logger
from app.utils.time_utils import utc_date_str, utcnow

logger = get_logger("risk.manager")


@dataclass
class RiskEvent:
    kind: str          # "DAILY_STOP" | "PAUSE_TRIGGERED" | "PAUSE_ENDED" | "CAPITAL_SNAPSHOT"
    message: str


class RiskManager:
    def __init__(self, settings: Settings, repository: TradeRepository):
        self._settings = settings
        self._repo = repository
        state = repository.get_risk_state()
        today = utc_date_str()
        if state is None or state.daily_date != today:
            state = RiskState(daily_date=today, daily_realized_pnl_usdt=0.0, daily_stopped=False,
                               consecutive_losses=(state.consecutive_losses if state else 0),
                               pause_until=(state.pause_until if state else None))
        self._state = state
        # Always re-derive the effective (possibly compounded) capital from
        # ground truth on startup -- safe even on a same-day restart, since
        # it is a pure function of settings + the immutable closed-trades
        # history, never an incrementally-drifting counter.
        self._recompute_effective_capital()
        self._repo.save_risk_state(self._state)
        # Suppresses a duplicate CAPITAL_SNAPSHOT notification for "today" --
        # the caller (core/bot.py) sends its own dedicated startup message
        # covering this initial snapshot; check_capital_snapshot_event() is
        # for detecting *later* day-boundary re-snapshots only.
        self._last_notified_capital_date = self._state.daily_date

    def _recompute_effective_capital(self) -> None:
        if self._settings.reinvest_profits:
            total_pnl = self._repo.get_total_realized_pnl_usdt()
            effective = max(self._settings.capital_usdt + total_pnl, 0.0)
        else:
            effective = self._settings.capital_usdt
        if effective != self._state.effective_capital_usdt:
            logger.info(
                "Effective trading capital snapshot: %.2f USDT (base=%.2f USDT, reinvest_profits=%s)",
                effective, self._settings.capital_usdt, self._settings.reinvest_profits,
            )
        self._state.effective_capital_usdt = effective

    # ------------------------------------------------------------- Sizing
    @property
    def capital_usdt(self) -> float:
        """The capital actually used for position sizing today -- the fixed
        baseline, or the daily-compounded snapshot when REINVEST_PROFITS=true."""
        self._reset_daily_if_needed()
        return self._state.effective_capital_usdt

    @property
    def per_trade_usdt(self) -> float:
        return self.capital_usdt * self._settings.trade_allocation_pct

    @property
    def max_open_positions(self) -> int:
        return self._settings.max_open_positions

    def compute_trade_capital(self, available_balance_usdt: float) -> Tuple[float, bool]:
        """Returns (capital_to_use, was_reduced). Never assumes funds that
        are not actually present in the account -- if the theoretical 48%
        allocation exceeds real available balance, it is capped down."""
        target = self.per_trade_usdt
        if available_balance_usdt < target:
            return max(available_balance_usdt, 0.0), True
        return target, False

    # ------------------------------------------------------- Daily bookkeeping
    def _reset_daily_if_needed(self) -> None:
        today = utc_date_str()
        if self._state.daily_date != today:
            self._state = RiskState(
                daily_date=today,
                daily_realized_pnl_usdt=0.0,
                daily_stopped=False,
                consecutive_losses=self._state.consecutive_losses,
                pause_until=self._state.pause_until,
                effective_capital_usdt=self._state.effective_capital_usdt,
            )
            # New trading day -> re-snapshot the (possibly compounded) capital.
            self._recompute_effective_capital()
            self._repo.save_risk_state(self._state)
            logger.info("Daily risk state reset for new UTC trading day %s", today)

    @property
    def is_daily_stopped(self) -> bool:
        self._reset_daily_if_needed()
        return self._state.daily_stopped

    @property
    def is_paused(self) -> bool:
        if not self._state.pause_until:
            return False
        pause_until = datetime.fromisoformat(self._state.pause_until)
        return utcnow() < pause_until

    @property
    def pause_until(self) -> Optional[datetime]:
        if not self._state.pause_until:
            return None
        return datetime.fromisoformat(self._state.pause_until)

    def can_open_new_trade(self, current_open_count: int) -> Tuple[bool, str]:
        self._reset_daily_if_needed()
        if self._state.daily_stopped:
            return False, "Daily Stop active: no new trades until the next UTC trading day"
        if self.is_paused:
            return False, f"Paused after consecutive losses until {self._state.pause_until} UTC"
        if current_open_count >= self._settings.max_open_positions:
            return False, f"Max open positions reached ({self._settings.max_open_positions})"
        return True, ""

    def register_closed_trade(self, pnl_usdt: float) -> list[RiskEvent]:
        """Call once, exactly once, per closed trade. Updates daily PnL and
        the consecutive-loss counter, and triggers Daily Stop / Pause as
        needed. Returns a list of events for the caller to notify about."""
        self._reset_daily_if_needed()
        events: list[RiskEvent] = []

        self._state.daily_realized_pnl_usdt += pnl_usdt
        is_loss = pnl_usdt < 0

        if is_loss:
            self._state.consecutive_losses += 1
        else:
            self._state.consecutive_losses = 0

        daily_stop_threshold = -abs(self._settings.daily_stop_pct) * self.capital_usdt
        if (not self._state.daily_stopped) and self._state.daily_realized_pnl_usdt <= daily_stop_threshold:
            self._state.daily_stopped = True
            events.append(RiskEvent(
                "DAILY_STOP",
                f"Daily Stop triggered: realized PnL {self._state.daily_realized_pnl_usdt:.2f} USDT "
                f"<= threshold {daily_stop_threshold:.2f} USDT. New entries blocked until next UTC day."
            ))

        if self._state.consecutive_losses >= self._settings.consecutive_loss_limit:
            pause_until = utcnow() + timedelta(minutes=self._settings.pause_minutes)
            self._state.pause_until = pause_until.isoformat()
            self._state.consecutive_losses = 0
            events.append(RiskEvent(
                "PAUSE_TRIGGERED",
                f"{self._settings.consecutive_loss_limit} consecutive losses reached. "
                f"New entries paused for {self._settings.pause_minutes} minutes, until {pause_until.isoformat()} UTC."
            ))

        self._repo.save_risk_state(self._state)
        return events

    def check_pause_expired_event(self) -> Optional[RiskEvent]:
        """Call periodically; returns a one-time PAUSE_ENDED event the moment
        an active pause naturally expires."""
        if self._state.pause_until and not self.is_paused:
            ended_at = self._state.pause_until
            self._state.pause_until = None
            self._repo.save_risk_state(self._state)
            return RiskEvent("PAUSE_ENDED", f"Pause window ended (was until {ended_at} UTC). Resuming new-entry scanning.")
        return None

    @property
    def daily_realized_pnl_usdt(self) -> float:
        self._reset_daily_if_needed()
        return self._state.daily_realized_pnl_usdt

    def check_capital_snapshot_event(self) -> Optional[RiskEvent]:
        """Call periodically; returns a one-time CAPITAL_SNAPSHOT event the
        moment a new UTC trading day re-snapshots the effective (compounded)
        capital -- lets the caller notify Telegram exactly once per day when
        the compounded size actually changes the trading capital."""
        self._reset_daily_if_needed()
        if self._state.daily_date != self._last_notified_capital_date:
            self._last_notified_capital_date = self._state.daily_date
            return RiskEvent(
                "CAPITAL_SNAPSHOT",
                f"New trading day {self._state.daily_date} UTC: effective capital = "
                f"{self._state.effective_capital_usdt:.2f} USDT "
                f"(base {self._settings.capital_usdt:.2f} USDT, reinvest_profits={self._settings.reinvest_profits}), "
                f"per-trade size = {self.per_trade_usdt:.2f} USDT."
            )
        return None
