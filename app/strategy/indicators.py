"""
Technical indicators, computed with pandas over a DataFrame of CLOSED candles
only (columns: open_time, open, high, low, close, volume), oldest-first.

All smoothing that Wilder originally defined (RSI, ATR, ADX/+DI/-DI) uses the
equivalent exponential form alpha = 1/period via pandas' `.ewm(adjust=False)`,
which is the standard, numerically-correct implementation of Wilder's RMA.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def _wilder_rma(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = _wilder_rma(gain, period)
    avg_loss = _wilder_rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    result = result.where(avg_loss != 0, 100.0)
    result = result.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return result


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def typical_price(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"] + df["close"]) / 3.0


def mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp = typical_price(df)
    raw_money_flow = tp * df["volume"]
    tp_diff = tp.diff()

    positive_flow = raw_money_flow.where(tp_diff > 0, 0.0)
    negative_flow = raw_money_flow.where(tp_diff < 0, 0.0)

    positive_sum = positive_flow.rolling(window=period, min_periods=period).sum()
    negative_sum = negative_flow.rolling(window=period, min_periods=period).sum()

    money_ratio = positive_sum / negative_sum.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + money_ratio))
    result = result.where(negative_sum != 0, 100.0)
    return result


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return _wilder_rma(tr, period)


def adx(df: pd.DataFrame, period: int = 14):
    """Returns (adx, plus_di, minus_di) using Wilder's original smoothing."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = true_range(df)
    atr_val = _wilder_rma(tr, period)

    smoothed_plus_dm = _wilder_rma(plus_dm, period)
    smoothed_minus_dm = _wilder_rma(minus_dm, period)

    plus_di = 100.0 * (smoothed_plus_dm / atr_val.replace(0.0, np.nan))
    minus_di = 100.0 * (smoothed_minus_dm / atr_val.replace(0.0, np.nan))

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_val = _wilder_rma(dx.fillna(0.0), period)

    return adx_val, plus_di.fillna(0.0), minus_di.fillna(0.0)


def compute_all(df: pd.DataFrame, settings) -> pd.DataFrame:
    """Attaches every indicator column the strategy needs to a copy of `df`."""
    out = df.copy()
    out["ema8"] = ema(out["close"], 8)
    out["ema200"] = ema(out["close"], 200)
    out["sma85"] = sma(out["close"], 85)
    out["sma20_volume"] = sma(out["volume"].shift(1), 20)

    macd_line, macd_signal, macd_hist = macd(out["close"])
    out["macd"] = macd_line
    out["macd_signal"] = macd_signal
    out["macd_hist"] = macd_hist

    out["rsi14"] = rsi(out["close"], 14)
    out["mfi14"] = mfi(out, 14)

    adx_val, plus_di, minus_di = adx(out, 14)
    out["adx14"] = adx_val
    out["plus_di14"] = plus_di
    out["minus_di14"] = minus_di

    out["atr14"] = atr(out, settings.atr_period if settings else 14)
    out["typical_price"] = typical_price(out)

    return out
