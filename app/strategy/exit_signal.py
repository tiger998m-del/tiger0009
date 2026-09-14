"""
Exit constitution ("دستور وقف الخسارة وإدارة الخروج"):

Method 1 -- ATR Trailing Stop (computed on closed candles only, executed
instantly against live price -- the live-price comparison happens in
app/execution/position_manager.py, NOT here):
    Candidate SL = Close[-1] - ATR14[-1] * 1.5
    Active SL   = MAX(Previous Active SL, Candidate SL)
The stop can only ever move up, never down.

Method 2 -- EMA8 breakdown exit, all 3 conditions must hold on the SAME
closed 5m candle:
    a) bearish cross: previous candle closed above its own EMA8, this candle
       closes below its own EMA8.
    b) this candle's close < the lowest low of the 4 preceding closed candles.
    c) this candle's volume > volume of each of the 2 preceding candles.

Either method alone is sufficient to close the trade; they are independent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from app.strategy.indicators import compute_all


def compute_active_atr_stop(
    df_closed: pd.DataFrame, settings, previous_active_sl: Optional[float]
) -> float:
    ind = compute_all(df_closed, settings)
    row = ind.iloc[-1]
    atr14 = float(row["atr14"])
    close = float(row["close"])
    candidate_sl = close - atr14 * settings.atr_multiplier
    if previous_active_sl is None:
        return candidate_sl
    return max(previous_active_sl, candidate_sl)


@dataclass
class Ema8ExitResult:
    triggered: bool
    reason: str = ""


def check_ema8_break_exit(df_closed: pd.DataFrame, settings) -> Ema8ExitResult:
    if len(df_closed) < 7:
        return Ema8ExitResult(False, "insufficient history for EMA8 exit check")

    ind = compute_all(df_closed, settings)
    row = ind.iloc[-1]
    prev = ind.iloc[-2]

    close = float(row["close"])
    volume = float(row["volume"])

    # a) bearish cross of EMA8
    if pd.isna(prev["ema8"]) or pd.isna(row["ema8"]):
        return Ema8ExitResult(False, "EMA8 not yet available")
    bearish_cross = (prev["close"] > prev["ema8"]) and (close < row["ema8"])
    if not bearish_cross:
        return Ema8ExitResult(False, "no bearish EMA8 cross on this candle")

    # b) close below the low of the preceding 4 complete candles
    prev4_low = ind["low"].iloc[-5:-1].min()
    if pd.isna(prev4_low) or not (close < prev4_low):
        return Ema8ExitResult(False, "close not below low of preceding 4 candles")

    # c) volume greater than each of the preceding 2 candles
    prev2_vol = ind["volume"].iloc[-3:-1]
    if len(prev2_vol) < 2 or not (volume > prev2_vol.max()):
        return Ema8ExitResult(False, "volume not greater than each of preceding 2 candles")

    return Ema8ExitResult(True, "EMA8 breakdown exit: bearish cross + lower low + higher volume confirmed")
