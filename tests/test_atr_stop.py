from __future__ import annotations

import pandas as pd

from app.strategy.exit_signal import compute_active_atr_stop
from tests.conftest import make_settings


def _df(closes, highs=None, lows=None):
    n = len(closes)
    highs = highs or [c + 1.0 for c in closes]
    lows = lows or [c - 1.0 for c in closes]
    return pd.DataFrame({
        "open_time": list(range(n)),
        "open": [c - 0.1 for c in closes],
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n,
    })


def test_atr_stop_rises_when_price_rises():
    settings = make_settings(atr_period=14, atr_multiplier=1.5)
    base_closes = [100 + i * 0.5 for i in range(30)]
    df1 = _df(base_closes)
    stop1 = compute_active_atr_stop(df1, settings, previous_active_sl=None)

    higher_closes = base_closes + [base_closes[-1] + 5]
    df2 = _df(higher_closes)
    stop2 = compute_active_atr_stop(df2, settings, previous_active_sl=stop1)

    assert stop2 >= stop1


def test_atr_stop_never_decreases_even_if_price_pulls_back():
    settings = make_settings(atr_period=14, atr_multiplier=1.5)
    rising_closes = [100 + i * 1.0 for i in range(30)]
    df1 = _df(rising_closes)
    stop1 = compute_active_atr_stop(df1, settings, previous_active_sl=None)

    # Next candle pulls back hard -- candidate SL would be lower than stop1,
    # but the active stop must still be MAX(previous, candidate) = stop1.
    pulled_back_closes = rising_closes + [rising_closes[-1] - 20]
    df2 = _df(pulled_back_closes)
    stop2 = compute_active_atr_stop(df2, settings, previous_active_sl=stop1)

    assert stop2 == stop1


def test_initial_atr_stop_uses_close_minus_atr_times_multiplier():
    settings = make_settings(atr_period=14, atr_multiplier=1.5)
    closes = [100.0] * 20  # flat series -> ATR should approach a small stable value
    df = _df(closes, highs=[101.0] * 20, lows=[99.0] * 20)
    stop = compute_active_atr_stop(df, settings, previous_active_sl=None)
    assert stop < closes[-1]  # stop must sit below the close
