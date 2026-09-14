"""
Signal Engine: the orchestration layer that sits between raw closed-candle
data (CandleStore) and the entry/exit rule modules. This is the only place
that decides *when* to run entry/exit evaluation, keeping the rule logic
itself (entry_signal.py / exit_signal.py) pure and independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from app.strategy.entry_signal import EntrySignalResult, evaluate_entry
from app.strategy.exit_signal import Ema8ExitResult, check_ema8_break_exit, compute_active_atr_stop


@dataclass
class ExitEvaluation:
    new_active_sl: float
    ema8_exit: Ema8ExitResult


class SignalEngine:
    def __init__(self, settings):
        self._settings = settings

    def evaluate_entry(self, df_closed: pd.DataFrame) -> EntrySignalResult:
        """Runs the full 15+ condition BUY constitution on the latest closed candle."""
        return evaluate_entry(df_closed, self._settings)

    def evaluate_exit(self, df_closed: pd.DataFrame, previous_active_sl: Optional[float]) -> ExitEvaluation:
        """Recomputes the ATR trailing stop (never decreases) and checks the
        EMA8 breakdown exit, both based strictly on the latest CLOSED candle."""
        new_active_sl = compute_active_atr_stop(df_closed, self._settings, previous_active_sl)
        ema8_result = check_ema8_break_exit(df_closed, self._settings)
        return ExitEvaluation(new_active_sl=new_active_sl, ema8_exit=ema8_result)
