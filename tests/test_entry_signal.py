from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategy.entry_signal import MIN_REQUIRED_CANDLES, evaluate_entry
from app.strategy.indicators import atr as compute_atr
from tests.conftest import make_settings


def _random_walk_df(n=260, seed=1, high_low_spread=1.0, atr_scale=1.0):
    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=0.05, scale=1.0, size=n)
    closes = 100 + np.cumsum(steps)
    opens = closes - rng.normal(0, 0.3, size=n)
    highs = np.maximum(opens, closes) + high_low_spread * atr_scale
    lows = np.minimum(opens, closes) - high_low_spread * atr_scale
    volumes = rng.uniform(1000, 2000, size=n)
    return pd.DataFrame({
        "open_time": list(range(n)),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def test_insufficient_history_rejected_without_crashing():
    settings = make_settings()
    df = _random_walk_df(n=50)
    result = evaluate_entry(df, settings)
    assert result.passed is False
    assert "insufficient" in result.reasons_failed[0]


def test_min_required_candles_constant_is_sane():
    assert MIN_REQUIRED_CANDLES >= 200  # must comfortably cover EMA200 warm-up


def test_initial_sl_and_risk_reward_formulas_are_consistent():
    settings = make_settings(atr_multiplier=1.5, take_profit_pct=0.03)
    df = _random_walk_df(n=260, high_low_spread=0.5)
    result = evaluate_entry(df, settings)

    close = df["close"].iloc[-1]
    atr_series = compute_atr(df, settings.atr_period)
    expected_atr = atr_series.iloc[-1]
    expected_initial_sl = close - expected_atr * settings.atr_multiplier
    expected_sl_pct = (close - expected_initial_sl) / close * 100.0
    expected_rr = (settings.take_profit_pct * 100.0) / expected_sl_pct

    assert abs(result.initial_sl - expected_initial_sl) < 1e-6
    assert abs(result.initial_sl_pct - expected_sl_pct) < 1e-6
    assert abs(result.risk_reward - expected_rr) < 1e-6


def test_wide_atr_relative_to_price_is_rejected_for_excessive_sl_percent():
    settings = make_settings(atr_multiplier=1.5, initial_sl_max_pct=3.0)
    # Huge high/low spread -> huge ATR -> Initial SL% will blow past 3%.
    df = _random_walk_df(n=260, high_low_spread=20.0)
    result = evaluate_entry(df, settings)
    assert result.passed is False
    assert any("Initial SL%" in r for r in result.reasons_failed)


def test_tiny_atr_relative_to_price_does_not_trigger_sl_percent_rejection():
    settings = make_settings(atr_multiplier=1.5, initial_sl_max_pct=3.0, min_risk_reward=1.25)
    # Small, tight high/low spread -> tiny ATR -> Initial SL% comfortably below 3%.
    df = _random_walk_df(n=260, high_low_spread=0.05)
    result = evaluate_entry(df, settings)
    assert not any("Initial SL%" in r for r in result.reasons_failed)
    assert not any("Risk/Reward" in r for r in result.reasons_failed)
