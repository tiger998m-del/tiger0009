"""
Shared WebSocket connection/reconnect machinery used by both the kline
stream and the price/ticker stream.

Features:
  - Combined-stream connection (wss://.../stream?streams=a@x/b@y/...)
  - Automatic reconnect with capped exponential backoff on any disconnect,
    error, or prolonged silence (heartbeat/staleness watchdog)
  - Resubscription on reconnect is implicit: the combined-stream URL is
    rebuilt from the current symbol list every time we (re)connect, so a
    fresh connection always carries the full, current subscription set.
  - Dynamic symbol list updates (`update_symbols`) force a clean reconnect
    so a changed universe / changed open-position set takes effect quickly.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Iterable, List, Optional

import websockets

from app.utils.logging_setup import get_logger

logger = get_logger("marketdata.ws")

_MAX_BACKOFF_SECONDS = 60.0
_STALE_AFTER_SECONDS = 180.0  # if no message at all for 3 minutes, force reconnect


class ReconnectingCombinedStream:
    def __init__(
        self,
        ws_base_url: str,
        stream_name_builder: Callable[[str], str],
        on_message: Callable[[dict], "asyncio.Future"],
        stream_label: str,
        on_disconnected=None,
        on_reconnected=None,
    ):
        self._ws_base_url = ws_base_url.rstrip("/")
        self._stream_name_builder = stream_name_builder
        self._on_message = on_message
        self._label = stream_label
        self._on_disconnected = on_disconnected
        self._on_reconnected = on_reconnected

        self._symbols: List[str] = []
        self._symbols_lock = asyncio.Lock()
        self._want_reconnect = asyncio.Event()
        self._stopped = False
        self._last_message_at = time.monotonic()
        self._connected_once = False

    async def update_symbols(self, symbols: Iterable[str]) -> None:
        new_symbols = sorted(set(symbols))
        async with self._symbols_lock:
            if new_symbols != self._symbols:
                self._symbols = new_symbols
                self._want_reconnect.set()

    def _build_url(self) -> Optional[str]:
        if not self._symbols:
            return None
        streams = "/".join(self._stream_name_builder(s) for s in self._symbols)
        return f"{self._ws_base_url}/stream?streams={streams}"

    async def stop(self) -> None:
        self._stopped = True
        self._want_reconnect.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stopped:
            self._want_reconnect.clear()
            async with self._symbols_lock:
                url = self._build_url()
            if url is None:
                await asyncio.sleep(1.0)
                continue

            try:
                async with websockets.connect(
                    url, ping_interval=180, ping_timeout=60, close_timeout=5
                ) as ws:
                    if self._connected_once and self._on_reconnected:
                        await self._on_reconnected(self._label)
                    self._connected_once = True
                    backoff = 1.0
                    self._last_message_at = time.monotonic()
                    logger.info("%s connected (%d symbols)", self._label, len(self._symbols))

                    watchdog = asyncio.create_task(self._watchdog(ws))
                    try:
                        async for raw in ws:
                            self._last_message_at = time.monotonic()
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            data = payload.get("data", payload)
                            await self._on_message(data)
                            if self._want_reconnect.is_set():
                                break
                    finally:
                        watchdog.cancel()
            except Exception as exc:
                logger.warning("%s connection error: %s", self._label, exc)
                if self._on_disconnected:
                    try:
                        await self._on_disconnected(self._label)
                    except Exception:
                        pass

            if self._stopped:
                break
            logger.info("%s reconnecting in %.1fs", self._label, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

    async def _watchdog(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                if time.monotonic() - self._last_message_at > _STALE_AFTER_SECONDS:
                    logger.warning("%s stale (no messages for >%.0fs); forcing reconnect",
                                   self._label, _STALE_AFTER_SECONDS)
                    await ws.close()
                    return
        except asyncio.CancelledError:
            return
