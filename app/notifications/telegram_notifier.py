"""
Telegram notifications.

Actual delivery relies ONLY on TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (never
on TELEGRAM_USERNAME, which is informational/for-humans only, per
constitution #8). If the token/chat id are not configured, notifications are
silently disabled (logged once) so the bot can still run -- it must not
crash just because Telegram hasn't been wired up yet.

The bot token is never logged or printed, even in redacted form beyond the
generic SecretRedactionFilter already scrubbing it at the logging layer.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import aiohttp

from app.utils.logging_setup import get_logger

logger = get_logger("notifications.telegram")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, username: str,
                 session: Optional[aiohttp.ClientSession] = None):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._username = username
        self._session = session
        self._owns_session = session is None
        self._enabled = bool(bot_token) and bool(chat_id)
        if not self._enabled:
            logger.warning(
                "Telegram notifications DISABLED: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID "
                "are not set in .env. Configure them to enable remote monitoring for %s.",
                username or "(no username set)",
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if self._enabled and self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def send(self, text: str, max_retries: int = 3) -> None:
        if not self._enabled:
            logger.debug("Telegram disabled, message suppressed: %s", text)
            return
        if self._session is None:
            await self.start()

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text}

        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                async with self._session.post(url, data=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return
                    body = await resp.text()
                    logger.warning(
                        "Telegram sendMessage failed HTTP %s (attempt %s/%s): %s",
                        resp.status, attempt, max_retries, body,
                    )
            except Exception as exc:
                logger.warning("Telegram sendMessage error (attempt %s/%s): %s", attempt, max_retries, exc)
            await asyncio.sleep(backoff)
            backoff *= 2
        logger.error("Telegram sendMessage ultimately failed after %s attempts; message dropped: %s",
                      max_retries, text)


class NotificationMessages:
    """Canonical message builders so wording stays consistent everywhere."""

    @staticmethod
    def bot_started(username: str, mode_text: str, capital_sar: float, capital_usdt: float) -> str:
        return (
            "BOT STARTED\n"
            f"Telegram: {username}\n"
            f"Mode: {mode_text}\n"
            f"Capital: {capital_sar:.2f} SAR (~{capital_usdt:.2f} USDT)"
        )

    @staticmethod
    def bot_stopped(reason: str = "") -> str:
        return f"BOT STOPPED\n{reason}".strip()

    @staticmethod
    def mode_banner(dry_run: bool) -> str:
        return "DRY RUN MODE\nNO REAL ORDERS" if dry_run else "LIVE TRADING MODE\nREAL MONEY ENABLED"

    @staticmethod
    def capital_snapshot(base_usdt: float, effective_usdt: float, per_trade_usdt: float,
                          reinvest_enabled: bool) -> str:
        if not reinvest_enabled:
            return (
                "CAPITAL SNAPSHOT\n"
                f"Reinvestment: DISABLED (fixed baseline)\n"
                f"Trading Capital: {effective_usdt:.2f} USDT\n"
                f"Per-Trade Size (48%): {per_trade_usdt:.2f} USDT"
            )
        delta = effective_usdt - base_usdt
        sign = "+" if delta >= 0 else ""
        return (
            "CAPITAL SNAPSHOT (Daily Reinvestment)\n"
            f"Baseline: {base_usdt:.2f} USDT\n"
            f"Cumulative PnL: {sign}{delta:.2f} USDT\n"
            f"Effective Trading Capital Today: {effective_usdt:.2f} USDT\n"
            f"Per-Trade Size (48%): {per_trade_usdt:.2f} USDT"
        )

    @staticmethod
    def entry_signal_accepted(symbol: str, close: float, sl_pct: float, rr: float) -> str:
        return (f"ENTRY SIGNAL ACCEPTED\nSymbol: {symbol}\nClose: {close}\n"
                f"Initial SL%: {sl_pct:.2f}%\nRisk/Reward: {rr:.2f}")

    @staticmethod
    def entry_signal_rejected(symbol: str, reasons: list[str]) -> str:
        top = "; ".join(reasons[:3])
        return f"ENTRY REJECTED\nSymbol: {symbol}\nReason(s): {top}"

    @staticmethod
    def max_positions_reached(symbol: str) -> str:
        return f"MAX OPEN POSITIONS REACHED\nSkipped new signal on {symbol}"

    @staticmethod
    def buy_order_sent(symbol: str, capital_usdt: float) -> str:
        return f"BUY ORDER SENT\nSymbol: {symbol}\nAllocated: {capital_usdt:.2f} USDT"

    @staticmethod
    def buy_executed(symbol: str, avg_price: float, qty: float, capital_usdt: float,
                      tp_price: float, atr_stop: float) -> str:
        return (
            "BUY EXECUTED\n"
            f"Symbol: {symbol}\n"
            f"Entry Price: {avg_price}\n"
            f"Quantity: {qty}\n"
            f"Position Size: {capital_usdt:.2f} USDT\n"
            f"TP: {tp_price}\n"
            f"Initial ATR Stop: {atr_stop}"
        )

    @staticmethod
    def order_rejected_by_filters(symbol: str, reason: str) -> str:
        return f"ORDER REJECTED (Binance Filters)\nSymbol: {symbol}\nReason: {reason}"

    @staticmethod
    def atr_stop_updated(symbol: str, new_stop: float) -> str:
        return f"ATR STOP UPDATED\nSymbol: {symbol}\nNew Active Stop: {new_stop}"

    @staticmethod
    def take_profit_executed(symbol: str, price: float, pnl: float) -> str:
        return f"TAKE PROFIT EXECUTED\nSymbol: {symbol}\nExit Price: {price}\nPnL: {pnl:.2f} USDT"

    @staticmethod
    def stop_loss_executed(symbol: str, price: float, pnl: float) -> str:
        return f"ATR STOP LOSS EXECUTED\nSymbol: {symbol}\nExit Price: {price}\nPnL: {pnl:.2f} USDT"

    @staticmethod
    def exit_executed(symbol: str, reason: str, price: float, pnl: float) -> str:
        return f"EXIT EXECUTED ({reason})\nSymbol: {symbol}\nExit Price: {price}\nPnL: {pnl:.2f} USDT"

    @staticmethod
    def api_error(context: str, message: str) -> str:
        return f"API ERROR\nContext: {context}\nMessage: {message}"

    @staticmethod
    def websocket_disconnected(stream: str) -> str:
        return f"WEBSOCKET DISCONNECTED\nStream: {stream}"

    @staticmethod
    def websocket_reconnected(stream: str) -> str:
        return f"WEBSOCKET RECONNECTED\nStream: {stream}"

    @staticmethod
    def daily_stop_triggered(message: str) -> str:
        return f"DAILY STOP ACTIVATED\n{message}"

    @staticmethod
    def pause_triggered(message: str) -> str:
        return f"PAUSE ACTIVATED\n{message}"

    @staticmethod
    def pause_ended(message: str) -> str:
        return f"PAUSE ENDED - RESUMING\n{message}"

    @staticmethod
    def recovery_started() -> str:
        return "RECOVERY STARTED\nReconciling local database with Binance account state..."

    @staticmethod
    def recovery_completed(open_trades: int, mismatches: int) -> str:
        return f"RECOVERY COMPLETED\nOpen trades restored: {open_trades}\nMismatches flagged: {mismatches}"

    @staticmethod
    def state_mismatch(symbol: str, details: str) -> str:
        return f"STATE MISMATCH DETECTED\nSymbol: {symbol}\nDetails: {details}\nManual review required."
