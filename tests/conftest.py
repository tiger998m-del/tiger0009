from __future__ import annotations

import pytest

from app.config.settings import Settings


def make_settings(**overrides) -> Settings:
    defaults = dict(
        binance_api_key="",
        binance_api_secret="",
        binance_base_url="https://api.binance.com",
        binance_ws_base_url="wss://stream.binance.com:9443",
        recv_window_ms=5000,
        time_drift_max_ms=1000,
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_username="@tiger007KSA",
        bot_capital_sar=1000.0,
        sar_per_usdt=3.75,
        reinvest_profits=True,
        dry_run=True,
        trade_allocation_pct=0.48,
        max_open_positions=2,
        quote_asset="USDT",
        take_profit_pct=0.03,
        atr_period=14,
        atr_multiplier=1.5,
        initial_sl_max_pct=3.0,
        min_risk_reward=1.25,
        min_market_cap_usd=50_000_000.0,
        min_volume_24h_usd=50_000_000.0,
        universe_refresh_minutes=60,
        universe_max_symbols=40,
        market_cap_check_enabled=True,
        coingecko_base_url="https://api.coingecko.com/api/v3",
        market_cap_cache_minutes=30,
        daily_stop_pct=0.02,
        consecutive_loss_limit=2,
        pause_minutes=60,
        kline_interval="5m",
        candle_history_limit=300,
        order_poll_interval_seconds=0.01,
        order_poll_timeout_seconds=1.0,
        tp_poll_interval_seconds=0.01,
        log_level="INFO",
        log_dir="logs",
        log_max_bytes=1024 * 1024,
        log_backup_count=1,
        db_path=":memory:",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings() -> Settings:
    return make_settings()
