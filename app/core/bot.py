"""
Core orchestrator. Wires configuration, exchange access, market data,
strategy, risk, execution, persistence and notifications together, and runs
the bot's event-driven main loop until asked to shut down.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Dict, Set

import aiohttp
import pandas as pd

from app.config.settings import Settings, load_settings
from app.database.repository import TradeRepository
from app.exchange.rest_client import BinanceRestClient
from app.exchange.symbol_filters import SymbolFilters
from app.execution.order_executor import OrderExecutor
from app.execution.position_manager import PositionManager
from app.core.recovery import run_recovery
from app.marketdata.candle_store import CandleStore
from app.marketdata.kline_stream import KlineStreamManager
from app.marketdata.price_stream import PriceStreamManager
from app.notifications.telegram_notifier import NotificationMessages, TelegramNotifier
from app.risk.risk_manager import RiskManager
from app.safety.startup_checks import run_startup_checks
from app.strategy.entry_signal import MIN_REQUIRED_CANDLES
from app.strategy.signal_engine import SignalEngine
from app.symbols.market_cap_provider import MarketCapProvider
from app.symbols.universe import build_universe, load_all_symbol_filters
from app.utils.logging_setup import get_logger, setup_logging

logger = get_logger("core.bot")


class TradingBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._rest: BinanceRestClient | None = None
        self._repo: TradeRepository | None = None
        self._risk: RiskManager | None = None
        self._notifier: TelegramNotifier | None = None
        self._market_cap_provider: MarketCapProvider | None = None
        self._candle_store = CandleStore(max_len=settings.candle_history_limit)
        self._signal_engine = SignalEngine(settings)
        self._filters_map: Dict[str, SymbolFilters] = {}
        self._executor: OrderExecutor | None = None
        self._position_manager: PositionManager | None = None
        self._kline_stream: KlineStreamManager | None = None
        self._price_stream: PriceStreamManager | None = None

        self._background_tasks: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()
        self._universe_symbols: Set[str] = set()

    # ------------------------------------------------------------- startup
    async def start(self) -> None:
        print("=" * 20)
        if self.settings.dry_run:
            print("DRY RUN MODE")
            print("NO REAL ORDERS")
        else:
            print("LIVE TRADING MODE")
            print("REAL MONEY ENABLED")
        print("=" * 20)

        self._session = aiohttp.ClientSession()
        self._rest = BinanceRestClient(
            api_key=self.settings.binance_api_key,
            api_secret=self.settings.binance_api_secret,
            base_url=self.settings.binance_base_url,
            recv_window_ms=self.settings.recv_window_ms,
            session=self._session,
        )
        self._notifier = TelegramNotifier(
            self.settings.telegram_bot_token, self.settings.telegram_chat_id,
            self.settings.telegram_username, session=self._session,
        )
        await self._notifier.start()

        mode_text = "LIVE TRADING MODE" if not self.settings.dry_run else "DRY RUN MODE"
        await self._notifier.send(NotificationMessages.bot_started(
            self.settings.telegram_username, mode_text, self.settings.bot_capital_sar, self.settings.capital_usdt,
        ))
        await self._notifier.send(NotificationMessages.mode_banner(self.settings.dry_run))

        report = await run_startup_checks(self.settings, self._rest)
        if not report.passed:
            failed = [f"{i.name}: {i.detail}" for i in report.items if i.critical and not i.ok]
            logger.error("Startup safety checks FAILED: %s", "; ".join(failed))
            await self._notifier.send("STARTUP SAFETY CHECK FAILED\n" + "\n".join(failed) +
                                       "\nBot will not start trading.")
            await self._cleanup()
            raise SystemExit(1)

        self._repo = TradeRepository(self.settings.db_path)
        self._risk = RiskManager(self.settings, self._repo)
        self._market_cap_provider = MarketCapProvider(
            self.settings.coingecko_base_url, self.settings.market_cap_cache_minutes, self._session,
        )
        self._executor = OrderExecutor(
            self._rest, self.settings.dry_run, self.settings.quote_asset,
            self.settings.order_poll_interval_seconds, self.settings.order_poll_timeout_seconds,
        )

        self._filters_map = await load_all_symbol_filters(self._rest)
        self._position_manager = PositionManager(
            self.settings, self._repo, self._risk, self._executor, self._rest,
            self._notifier, self._signal_engine, self._filters_map,
        )

        await run_recovery(self.settings, self._rest, self._repo, self._position_manager,
                            self._risk, self._notifier, self._filters_map)

        await self._refresh_universe()
        await self._seed_history(self._universe_symbols | set(self._position_manager.open_symbols()))

        self._kline_stream = KlineStreamManager(
            self.settings.binance_ws_base_url, self.settings.kline_interval, self._candle_store,
            on_candle_closed=self._on_candle_closed,
            on_disconnected=self._on_ws_disconnected,
            on_reconnected=self._on_ws_reconnected,
        )
        self._price_stream = PriceStreamManager(
            self.settings.binance_ws_base_url,
            on_price_tick=self._on_price_tick,
            on_disconnected=self._on_ws_disconnected,
            on_reconnected=self._on_ws_reconnected,
        )
        await self._kline_stream.update_symbols(self._universe_symbols | set(self._position_manager.open_symbols()))
        await self._price_stream.update_symbols(self._position_manager.open_symbols())

        self._background_tasks.append(asyncio.create_task(self._kline_stream.run(), name="kline_stream"))
        self._background_tasks.append(asyncio.create_task(self._price_stream.run(), name="price_stream"))
        self._background_tasks.append(asyncio.create_task(self._universe_refresh_loop(), name="universe_refresh"))
        self._background_tasks.append(asyncio.create_task(self._price_symbol_sync_loop(), name="price_symbol_sync"))
        self._background_tasks.append(asyncio.create_task(self._pause_watch_loop(), name="pause_watch"))

        logger.info("Bot started successfully. Mode=%s Capital=%.2f SAR (~%.2f USDT) Universe=%d symbols",
                     mode_text, self.settings.bot_capital_sar, self.settings.capital_usdt, len(self._universe_symbols))

    async def run_forever(self) -> None:
        self._install_signal_handlers()
        await self._shutdown_event.wait()
        await self._cleanup()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop("signal received")))
            except NotImplementedError:
                pass  # Windows / restricted environments

    async def stop(self, reason: str = "") -> None:
        logger.info("Shutdown requested: %s", reason)
        self._shutdown_event.set()

    # ------------------------------------------------------------- cleanup
    async def _cleanup(self) -> None:
        if self._notifier:
            await self._notifier.send(NotificationMessages.bot_stopped("Shutting down gracefully."))
        if self._kline_stream:
            await self._kline_stream.stop()
        if self._price_stream:
            await self._price_stream.stop()
        for task in self._background_tasks:
            task.cancel()
        for task in self._background_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._notifier:
            await self._notifier.close()
        if self._rest:
            await self._rest.close()
        if self._repo:
            self._repo.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Shutdown complete.")

    # --------------------------------------------------------------- events
    async def _on_candle_closed(self, symbol: str, candle: dict) -> None:
        df = self._candle_store.get_dataframe(symbol)
        if df is None or len(df) < MIN_REQUIRED_CANDLES:
            return

        if symbol in self._position_manager.open_symbols():
            await self._position_manager.on_candle_closed(symbol, df)
            return

        allowed, reason = self._risk.can_open_new_trade(self._position_manager.open_count())
        if not allowed:
            logger.debug("New-entry scanning blocked for %s: %s", symbol, reason)
            return

        entry_result = self._signal_engine.evaluate_entry(df)
        if entry_result.passed:
            logger.info("Entry signal ACCEPTED for %s: SL%%=%.3f RR=%.3f",
                         symbol, entry_result.initial_sl_pct, entry_result.risk_reward)
            await self._notifier.send(NotificationMessages.entry_signal_accepted(
                symbol, entry_result.close, entry_result.initial_sl_pct, entry_result.risk_reward
            ))
            await self._position_manager.try_enter(symbol, entry_result)
        else:
            technical_condition_failed = any(r.startswith("C") for r in entry_result.reasons_failed)
            logger.debug("Entry signal rejected for %s: %s", symbol, "; ".join(entry_result.reasons_failed))
            if not technical_condition_failed and entry_result.reasons_failed:
                # Passed every technical condition but failed the risk gate
                # (SL% > 3% or RR < 1.25) -- worth a real-time alert, unlike
                # the routine per-candle technical misses which would spam.
                await self._notifier.send(
                    NotificationMessages.entry_signal_rejected(symbol, entry_result.reasons_failed)
                )

    async def _on_price_tick(self, symbol: str, price: float) -> None:
        await self._position_manager.on_price_tick(symbol, price)

    async def _on_ws_disconnected(self, stream_label: str) -> None:
        await self._notifier.send(NotificationMessages.websocket_disconnected(stream_label))

    async def _on_ws_reconnected(self, stream_label: str) -> None:
        await self._notifier.send(NotificationMessages.websocket_reconnected(stream_label))

    # ---------------------------------------------------------- background
    async def _refresh_universe(self) -> None:
        try:
            self._filters_map.clear()
            self._filters_map.update(await load_all_symbol_filters(self._rest))
            universe = await build_universe(self._rest, self._market_cap_provider, self.settings, self._filters_map)
            self._universe_symbols = {u.symbol for u in universe}
            logger.info("Universe refreshed: %d symbols", len(self._universe_symbols))
        except Exception:
            logger.exception("Universe refresh failed; keeping previous universe")

    async def _seed_history(self, symbols: Set[str]) -> None:
        for symbol in symbols:
            if self._candle_store.has_symbol(symbol):
                continue
            try:
                raw = await self._rest.klines(symbol, self.settings.kline_interval,
                                               self.settings.candle_history_limit + 1)
                # Drop the last kline: it is the currently-forming candle, never closed.
                closed = raw[:-1] if raw else []
                self._candle_store.seed(symbol, closed)
            except Exception:
                logger.exception("Failed to seed candle history for %s", symbol)

    async def _universe_refresh_loop(self) -> None:
        interval = self.settings.universe_refresh_minutes * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await self._refresh_universe()
                combined = self._universe_symbols | set(self._position_manager.open_symbols())
                await self._seed_history(combined)
                await self._kline_stream.update_symbols(combined)
            except Exception:
                logger.exception("Universe refresh loop iteration failed")

    async def _price_symbol_sync_loop(self) -> None:
        last: Set[str] = set()
        while True:
            await asyncio.sleep(3)
            try:
                current = set(self._position_manager.open_symbols())
                if current != last:
                    await self._price_stream.update_symbols(current)
                    combined = self._universe_symbols | current
                    await self._kline_stream.update_symbols(combined)
                    last = current
            except Exception:
                logger.exception("Price symbol sync loop iteration failed")

    async def _pause_watch_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                event = self._risk.check_pause_expired_event()
                if event:
                    await self._notifier.send(NotificationMessages.pause_ended(event.message))
            except Exception:
                logger.exception("Pause watch loop iteration failed")


async def create_and_run_bot(env_file: str | None = None) -> None:
    settings = load_settings(env_file)
    setup_logging(
        settings.log_dir, settings.log_level, settings.log_max_bytes, settings.log_backup_count,
        secrets=[settings.binance_api_key, settings.binance_api_secret, settings.telegram_bot_token],
    )
    bot = TradingBot(settings)
    await bot.start()
    await bot.run_forever()
