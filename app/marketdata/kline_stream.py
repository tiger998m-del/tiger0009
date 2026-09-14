"""
5-minute Kline WebSocket manager.

Only fully closed candles ("k"."x" == true) are ever forwarded to the
strategy layer -- an in-progress candle is parsed but discarded, satisfying
the constitution's "no signals from an unclosed candle" rule at the
transport layer itself, before the data ever reaches indicators/signals.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Iterable

from app.marketdata.candle_store import CandleStore
from app.marketdata.ws_base import ReconnectingCombinedStream
from app.utils.logging_setup import get_logger

logger = get_logger("marketdata.kline")

OnCandleClosed = Callable[[str, dict], Awaitable[None]]


class KlineStreamManager:
    def __init__(
        self,
        ws_base_url: str,
        interval: str,
        candle_store: CandleStore,
        on_candle_closed: OnCandleClosed,
        on_disconnected=None,
        on_reconnected=None,
    ):
        self._interval = interval
        self._candle_store = candle_store
        self._on_candle_closed = on_candle_closed

        self._stream = ReconnectingCombinedStream(
            ws_base_url=ws_base_url,
            stream_name_builder=lambda symbol: f"{symbol.lower()}@kline_{interval}",
            on_message=self._handle_message,
            stream_label="KlineStream",
            on_disconnected=on_disconnected,
            on_reconnected=on_reconnected,
        )

    async def update_symbols(self, symbols: Iterable[str]) -> None:
        await self._stream.update_symbols(symbols)

    async def run(self) -> None:
        await self._stream.run()

    async def stop(self) -> None:
        await self._stream.stop()

    async def _handle_message(self, data: dict) -> None:
        k = data.get("k")
        if not k:
            return
        is_closed = bool(k.get("x"))
        if not is_closed:
            return

        symbol = k.get("s")
        candle = {
            "open_time": int(k["t"]),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }
        added = self._candle_store.add_closed_candle(symbol, candle)
        if not added:
            logger.debug("Duplicate closed candle for %s open_time=%s ignored", symbol, candle["open_time"])
            return

        try:
            await self._on_candle_closed(symbol, candle)
        except Exception:
            logger.exception("Error handling closed candle for %s", symbol)
