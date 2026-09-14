"""Custom exceptions for the Binance exchange layer."""

from __future__ import annotations


class BinanceAPIError(RuntimeError):
    """Raised when Binance responds with an error payload or non-2xx status."""

    def __init__(self, status: int, code: int | None, message: str, path: str = ""):
        self.status = status
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"Binance API error on {path}: HTTP {status} code={code} msg={message}")


class BinanceConnectivityError(RuntimeError):
    """Raised when Binance REST/WebSocket cannot be reached at all."""


class OrderStateUncertainError(RuntimeError):
    """Raised when we cannot determine with confidence whether an order was
    filled/rejected. Callers MUST treat this as a hard-stop for further
    automated action on the affected symbol until reconciled -- see
    core/recovery.py and instruction #29 (capital protection over uptime)."""
