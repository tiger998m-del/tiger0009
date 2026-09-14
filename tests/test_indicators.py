from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategy.indicators import adx, atr, ema, mfi, rsi, sma


def _make_uptrend_df(n=60, start=100.0, step=1.0):
    closes = [start + i * step for i in range(n)]
    data = {
        "open_time": list(range(n)),
        "open": [c - 0.2 for c in closes],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0 + i for i in range(n)],
    }
    return pd.DataFrame(data)


def test_ema_converges_towards_price_in_steady_trend():
    df = _make_uptrend_df()
    e = ema(df["close"], 8)
    assert e.iloc[-1] < df["close"].iloc[-1]
    assert e.iloc[-1] > e.iloc[0]


def test_sma_matches_manual_average():
    df = _make_uptrend_df(n=30)
    s = sma(df["close"], 5)
    manual = df["close"].iloc[-5:].mean()
    assert abs(s.iloc[-1] - manual) < 1e-9


def test_rsi_is_high_in_strict_uptrend():
    df = _make_uptrend_df(n=60)
    r = rsi(df["close"], 14)
    assert r.iloc[-1] > 90  # strictly increasing closes -> RSI near 100


def test_rsi_is_low_in_strict_downtrend():
    df = _make_uptrend_df(n=60, start=200.0, step=-1.0)
    r = rsi(df["close"], 14)
    assert r.iloc[-1] < 10


def test_atr_is_positive_and_reacts_to_volatility():
    df = _make_uptrend_df(n=40)
    a = atr(df, 14)
    assert a.iloc[-1] > 0

    # Widen the last candle's range drastically and confirm ATR increases.
    df2 = df.copy()
    df2.loc[df2.index[-1], "high"] += 50
    df2.loc[df2.index[-1], "low"] -= 50
    a2 = atr(df2, 14)
    assert a2.iloc[-1] > a.iloc[-1]


def test_adx_plus_di_dominates_in_uptrend():
    df = _make_uptrend_df(n=60)
    adx_val, plus_di, minus_di = adx(df, 14)
    assert plus_di.iloc[-1] > minus_di.iloc[-1]


def test_mfi_high_in_uptrend_with_rising_volume():
    df = _make_uptrend_df(n=40)
    m = mfi(df, 14)
    assert m.iloc[-1] > 50
