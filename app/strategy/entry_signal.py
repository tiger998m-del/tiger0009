"""
Full BUY entry constitution, implemented exactly as specified, condition by
condition, with no simplification or omission:

Section 1 (handled upstream in app/symbols/universe.py before a symbol is
even considered here): Market Cap >= 50M, 24h Volume >= 50M, USDT Spot pair.

Section 2 (this module) -- 5m closed-candle technical conditions:
  C1  close > EMA8
  C2  close > EMA200
  C3  EMA8 crosses above SMA85 (from below to above)
  C4  MACD line > MACD signal line
  C5  RSI(14) crosses above 70 (from below to above)
  C6  RSI(14) < 93
  C7  MFI(14) > 50
  C8  +DI(14) crosses above 40 (from below to above)
  C9  ADX(14) > 20
  C10 ADX(14) < 60
  C11 -DI(14) < 20
  C12 candle is bullish: close > open
  C13 close > pivot = (H+L+C)/3 of the same candle
  C14 close > highest high of the preceding 5 closed candles
  C15 volume > volume of each of the preceding 3 closed candles
  C16 volume > 1.5 * SMA20(volume) computed over the 20 candles preceding
      the signal candle (current candle's own volume excluded from its
      baseline average)

Then, still before entry (constitution "دستور وقف الخسارة" items 2-4):
  - Initial SL is computed from ATR(14) on this same closed candle:
        Initial SL = close - ATR14 * 1.5
  - Initial SL% = (close - Initial SL) / close * 100
  - reject if Initial SL% > 3%
  - Risk/Reward = 3 / Initial SL% ; reject if RR < 1.25 (TP is fixed at +3%,
    exposed here as `settings.take_profit_pct`).

The minimum number of closed candles required is enforced by the caller
(EMA200 needs 200+ bars to be meaningful; we require CANDLE_HISTORY_LIMIT,
default 300, from settings).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pandas as pd

from app.strategy.indicators import compute_all

MIN_REQUIRED_CANDLES = 210


@dataclass
class EntrySignalResult:
    passed: bool
    reasons_failed: List[str] = field(default_factory=list)
    close: float = 0.0
    initial_sl: float = 0.0
    initial_sl_pct: float = 0.0
    risk_reward: float = 0.0
    atr14: float = 0.0


def evaluate_entry(df_closed: pd.DataFrame, settings) -> EntrySignalResult:
    if len(df_closed) < MIN_REQUIRED_CANDLES:
        return EntrySignalResult(
            passed=False,
            reasons_failed=[f"insufficient candle history ({len(df_closed)} < {MIN_REQUIRED_CANDLES})"],
        )

    ind = compute_all(df_closed, settings)
    row = ind.iloc[-1]
    prev = ind.iloc[-2]

    reasons: List[str] = []

    close = float(row["close"])
    open_ = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    volume = float(row["volume"])

    # C1
    if not (close > row["ema8"]):
        reasons.append("C1 failed: close <= EMA8")
    # C2
    if not (close > row["ema200"]):
        reasons.append("C2 failed: close <= EMA200")
    # C3: EMA8 crosses above SMA85
    if pd.isna(prev["sma85"]) or pd.isna(row["sma85"]):
        reasons.append("C3 failed: SMA85 not yet available")
    elif not (prev["ema8"] <= prev["sma85"] and row["ema8"] > row["sma85"]):
        reasons.append("C3 failed: EMA8 did not cross above SMA85 on this candle")
    # C4
    if not (row["macd"] > row["macd_signal"]):
        reasons.append("C4 failed: MACD line <= MACD signal line")
    # C5: RSI crosses above 70
    if pd.isna(prev["rsi14"]):
        reasons.append("C5 failed: RSI not yet available")
    elif not (prev["rsi14"] <= 70 and row["rsi14"] > 70):
        reasons.append("C5 failed: RSI(14) did not cross above 70 on this candle")
    # C6
    if not (row["rsi14"] < 93):
        reasons.append("C6 failed: RSI(14) >= 93")
    # C7
    if not (row["mfi14"] > 50):
        reasons.append("C7 failed: MFI(14) <= 50")
    # C8: +DI crosses above 40
    if pd.isna(prev["plus_di14"]):
        reasons.append("C8 failed: +DI not yet available")
    elif not (prev["plus_di14"] <= 40 and row["plus_di14"] > 40):
        reasons.append("C8 failed: +DI(14) did not cross above 40 on this candle")
    # C9
    if not (row["adx14"] > 20):
        reasons.append("C9 failed: ADX(14) <= 20")
    # C10
    if not (row["adx14"] < 60):
        reasons.append("C10 failed: ADX(14) >= 60")
    # C11
    if not (row["minus_di14"] < 20):
        reasons.append("C11 failed: -DI(14) >= 20")
    # C12
    if not (close > open_):
        reasons.append("C12 failed: candle is not bullish (close <= open)")
    # C13
    pivot = (high + low + close) / 3.0
    if not (close > pivot):
        reasons.append("C13 failed: close <= pivot (H+L+C)/3")
    # C14: close > highest high of preceding 5 candles
    prev5_high = ind["high"].iloc[-6:-1].max()
    if pd.isna(prev5_high) or not (close > prev5_high):
        reasons.append("C14 failed: close <= highest high of preceding 5 candles")
    # C15: volume > each of preceding 3 candles' volume
    prev3_vol = ind["volume"].iloc[-4:-1]
    if len(prev3_vol) < 3 or not (volume > prev3_vol.max()):
        reasons.append("C15 failed: volume <= volume of a preceding candle (of the last 3)")
    # C16: volume > 1.5x SMA20(volume) (baseline excludes current candle)
    if pd.isna(row["sma20_volume"]):
        reasons.append("C16 failed: 20-period average volume not yet available")
    elif not (volume > 1.5 * row["sma20_volume"]):
        reasons.append("C16 failed: volume <= 150% of SMA20(volume)")

    atr14 = float(row["atr14"]) if not pd.isna(row["atr14"]) else None
    if atr14 is None:
        reasons.append("ATR(14) not yet available")
        return EntrySignalResult(passed=False, reasons_failed=reasons, close=close)

    initial_sl = close - atr14 * settings.atr_multiplier
    initial_sl_pct = (close - initial_sl) / close * 100.0 if close > 0 else 100.0

    if initial_sl_pct > settings.initial_sl_max_pct:
        reasons.append(
            f"Initial SL% {initial_sl_pct:.3f}% exceeds max allowed {settings.initial_sl_max_pct}%"
        )

    tp_pct = settings.take_profit_pct * 100.0
    risk_reward = (tp_pct / initial_sl_pct) if initial_sl_pct > 0 else 0.0

    if risk_reward < settings.min_risk_reward:
        reasons.append(
            f"Risk/Reward {risk_reward:.3f} below minimum required {settings.min_risk_reward}"
        )

    return EntrySignalResult(
        passed=(len(reasons) == 0),
        reasons_failed=reasons,
        close=close,
        initial_sl=initial_sl,
        initial_sl_pct=initial_sl_pct,
        risk_reward=risk_reward,
        atr14=atr14,
    )
