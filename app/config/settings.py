"""
Central configuration loader.

Every tunable value that could conceivably need to change between deployments
(capital, thresholds, credentials, timing) lives in the ".env" file and is
loaded exactly once here. No financial parameter (capital, allocation %,
TP/SL thresholds, daily stop, pause duration, etc.) is ever hard-coded inside
strategy, risk or execution modules -- they all read from this Settings
object, which itself reads only from the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when the .env configuration is missing or invalid."""


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be a number, got: {raw!r}") from exc


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer, got: {raw!r}") from exc


def _get_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip()


@dataclass(frozen=True)
class Settings:
    # --- Binance credentials & endpoints -------------------------------
    binance_api_key: str
    binance_api_secret: str
    binance_base_url: str
    binance_ws_base_url: str
    recv_window_ms: int
    time_drift_max_ms: int

    # --- Telegram --------------------------------------------------------
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_username: str

    # --- Capital & currency conversion -----------------------------------
    bot_capital_sar: float
    sar_per_usdt: float

    # --- Trading mode ------------------------------------------------------
    dry_run: bool

    # --- Position sizing / limits -----------------------------------------
    trade_allocation_pct: float          # 0.48 => 48% of bot capital per trade
    max_open_positions: int              # 2
    quote_asset: str                     # "USDT"

    # --- Strategy targets ---------------------------------------------------
    take_profit_pct: float               # 0.03 => +3%
    atr_period: int                      # 14
    atr_multiplier: float                # 1.5
    initial_sl_max_pct: float            # 3.0 (percent, not fraction)
    min_risk_reward: float               # 1.25

    # --- Universe screening ---------------------------------------------
    min_market_cap_usd: float            # 50_000_000
    min_volume_24h_usd: float            # 50_000_000
    universe_refresh_minutes: int
    universe_max_symbols: int
    market_cap_check_enabled: bool
    coingecko_base_url: str
    market_cap_cache_minutes: int

    # --- Risk: daily stop / pause -----------------------------------------
    daily_stop_pct: float                # 0.02 => -2% of bot capital per day
    consecutive_loss_limit: int          # 2
    pause_minutes: int                   # 60

    # --- Candles / kline -----------------------------------------------
    kline_interval: str                  # "5m"
    candle_history_limit: int            # 300

    # --- Order execution ----------------------------------------------
    order_poll_interval_seconds: float
    order_poll_timeout_seconds: float
    tp_poll_interval_seconds: float

    # --- Logging ----------------------------------------------------------
    log_level: str
    log_dir: str
    log_max_bytes: int
    log_backup_count: int

    # --- Database -----------------------------------------------------------
    db_path: str

    def __post_init__(self) -> None:
        if self.bot_capital_sar <= 0:
            raise ConfigError("BOT_CAPITAL_SAR must be a positive number.")
        if self.sar_per_usdt <= 0:
            raise ConfigError("SAR_PER_USDT must be a positive number.")
        if not (0 < self.trade_allocation_pct <= 1):
            raise ConfigError("TRADE_ALLOCATION_PCT must be between 0 and 1.")
        if self.max_open_positions < 1:
            raise ConfigError("MAX_OPEN_POSITIONS must be at least 1.")
        if self.trade_allocation_pct * self.max_open_positions > 1.0 + 1e-9:
            raise ConfigError(
                "TRADE_ALLOCATION_PCT * MAX_OPEN_POSITIONS exceeds 100% of capital; "
                "this would risk overcommitting the account. Adjust .env."
            )
        if not self.dry_run:
            if not self.binance_api_key or not self.binance_api_secret:
                raise ConfigError(
                    "DRY_RUN=false (LIVE TRADING MODE) requires BINANCE_API_KEY and "
                    "BINANCE_API_SECRET to be set in .env."
                )

    @property
    def capital_usdt(self) -> float:
        """Bot-allocated capital, converted from SAR to USDT via SAR_PER_USDT."""
        return self.bot_capital_sar / self.sar_per_usdt

    @property
    def per_trade_usdt(self) -> float:
        return self.capital_usdt * self.trade_allocation_pct

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token) and bool(self.telegram_chat_id)


def load_settings(env_file: Optional[str] = None) -> Settings:
    """Load configuration from the environment / .env file.

    Call this exactly once at process startup. `env_file` may point to a
    specific .env path (used by tests); otherwise python-dotenv's default
    discovery (current working directory) is used.
    """
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)

    return Settings(
        binance_api_key=_get_str("BINANCE_API_KEY"),
        binance_api_secret=_get_str("BINANCE_API_SECRET"),
        binance_base_url=_get_str("BINANCE_BASE_URL", "https://api.binance.com"),
        binance_ws_base_url=_get_str("BINANCE_WS_BASE_URL", "wss://stream.binance.com:9443"),
        recv_window_ms=_get_int("RECV_WINDOW_MS", 5000),
        time_drift_max_ms=_get_int("TIME_DRIFT_MAX_MS", 1000),
        telegram_bot_token=_get_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get_str("TELEGRAM_CHAT_ID"),
        telegram_username=_get_str("TELEGRAM_USERNAME", "@tiger007KSA"),
        bot_capital_sar=_get_float("BOT_CAPITAL_SAR", 1000.0),
        sar_per_usdt=_get_float("SAR_PER_USDT", 3.75),
        dry_run=_get_bool("DRY_RUN", False),
        trade_allocation_pct=_get_float("TRADE_ALLOCATION_PCT", 0.48),
        max_open_positions=_get_int("MAX_OPEN_POSITIONS", 2),
        quote_asset=_get_str("QUOTE_ASSET", "USDT"),
        take_profit_pct=_get_float("TAKE_PROFIT_PCT", 0.03),
        atr_period=_get_int("ATR_PERIOD", 14),
        atr_multiplier=_get_float("ATR_MULTIPLIER", 1.5),
        initial_sl_max_pct=_get_float("INITIAL_SL_MAX_PCT", 3.0),
        min_risk_reward=_get_float("MIN_RISK_REWARD", 1.25),
        min_market_cap_usd=_get_float("MIN_MARKET_CAP_USD", 50_000_000.0),
        min_volume_24h_usd=_get_float("MIN_VOLUME_24H_USD", 50_000_000.0),
        universe_refresh_minutes=_get_int("UNIVERSE_REFRESH_MINUTES", 60),
        universe_max_symbols=_get_int("UNIVERSE_MAX_SYMBOLS", 40),
        market_cap_check_enabled=_get_bool("MARKET_CAP_CHECK_ENABLED", True),
        coingecko_base_url=_get_str("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3"),
        market_cap_cache_minutes=_get_int("MARKET_CAP_CACHE_MINUTES", 30),
        daily_stop_pct=_get_float("DAILY_STOP_PCT", 0.02),
        consecutive_loss_limit=_get_int("CONSECUTIVE_LOSS_LIMIT", 2),
        pause_minutes=_get_int("PAUSE_MINUTES", 60),
        kline_interval=_get_str("KLINE_INTERVAL", "5m"),
        candle_history_limit=_get_int("CANDLE_HISTORY_LIMIT", 300),
        order_poll_interval_seconds=_get_float("ORDER_POLL_INTERVAL_SECONDS", 2.0),
        order_poll_timeout_seconds=_get_float("ORDER_POLL_TIMEOUT_SECONDS", 30.0),
        tp_poll_interval_seconds=_get_float("TP_POLL_INTERVAL_SECONDS", 5.0),
        log_level=_get_str("LOG_LEVEL", "INFO"),
        log_dir=_get_str("LOG_DIR", "logs"),
        log_max_bytes=_get_int("LOG_MAX_BYTES", 5 * 1024 * 1024),
        log_backup_count=_get_int("LOG_BACKUP_COUNT", 5),
        db_path=_get_str("DB_PATH", "data/bot.db"),
    )
