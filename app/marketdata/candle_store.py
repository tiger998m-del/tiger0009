"""
In-memory ring buffer of CLOSED candles per symbol.

Guarantees:
  - Only fully closed candles (kline "x": true) are ever stored.
  - Duplicate candles (same open_time arriving twice because of a WebSocket
    reconnect/replay) are ignored -- this is the idempotency mechanism that
    prevents a re-delivered closed-candle event from re-triggering a signal
    that was already evaluated once.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict, List, Optional

import pandas as pd

_COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]


class CandleStore:
    def __init__(self, max_len: int = 300):
        self._max_len = max_len
        self._lock = threading.Lock()
        self._data: Dict[str, Deque[dict]] = {}
        self._last_open_time: Dict[str, int] = {}

    def seed(self, symbol: str, klines_raw: List[list]) -> None:
        """Seed history from a REST /klines response (list of
        [open_time, open, high, low, close, volume, close_time, ...]).
        Only fully closed candles should be passed -- callers must drop the
        last (still-open) kline from a REST response before calling this."""
        with self._lock:
            dq: Deque[dict] = deque(maxlen=self._max_len)
            last_ot = None
            for k in klines_raw:
                candle = {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
                dq.append(candle)
                last_ot = candle["open_time"]
            self._data[symbol] = dq
            if last_ot is not None:
                self._last_open_time[symbol] = last_ot

    def add_closed_candle(self, symbol: str, candle: dict) -> bool:
        """Returns True if the candle was newly added, False if it was a
        duplicate (already-seen open_time) and therefore ignored."""
        with self._lock:
            last_ot = self._last_open_time.get(symbol)
            if last_ot is not None and candle["open_time"] <= last_ot:
                return False
            if symbol not in self._data:
                self._data[symbol] = deque(maxlen=self._max_len)
            self._data[symbol].append(candle)
            self._last_open_time[symbol] = candle["open_time"]
            return True

    def get_dataframe(self, symbol: str) -> Optional[pd.DataFrame]:
        with self._lock:
            dq = self._data.get(symbol)
            if not dq:
                return None
            rows = list(dq)
        df = pd.DataFrame(rows, columns=_COLUMNS)
        return df

    def has_symbol(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._data and len(self._data[symbol]) > 0

    def length(self, symbol: str) -> int:
        with self._lock:
            return len(self._data.get(symbol, ()))

    def symbols(self) -> List[str]:
        with self._lock:
            return list(self._data.keys())
