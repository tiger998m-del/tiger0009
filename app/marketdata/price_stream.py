"""
Live price WebSocket manager (trade stream), used exclusively to detect the
instant an open position's price touches its Active ATR Stop so it can be
closed immediately without waiting for the next 5m candle close
(constitution item #12 -- the ONE exception to "closed candles only").

Subscribed symbols are exactly the symbols with a currently OPEN position
(at most MAX_OPEN_POSITIONS, i.e. 2), updated dynamically as positions
open/close.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Iterable

from app.marketdata.ws_base import ReconnectingCombinedStream
from app.utils.logging_setup import get_logger

logger = get_logger("marketdata.price")

OnPriceTick = Callable[[str, float], Awaitable[None]]


class PriceStreamManager:
    def __init__(self, ws_base_url: str, on_price_tick: OnPriceTick, on_disconnected=None, on_reconnected=None):
        self._on_price_tick = on_price_tick
        self._stream = ReconnectingCombinedStream(
            ws_base_url=ws_base_url,
            stream_name_builder=lambda symbol: f"{symbol.lower()}@trade",
            on_message=self._handle_message,
            stream_label="PriceStream",
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
        symbol = data.get("s")
        price_raw = data.get("p")
        if not symbol or price_raw is None:
            return
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            return
        try:
            await self._on_price_tick(symbol, price)
        except Exception:
            logger.exception("Error handling price tick for %s", symbol)
