"""
Async Binance Spot REST client.

Only the endpoints this bot actually needs are implemented: market data,
account/balance, order placement (Spot only, MARKET/LIMIT), order status,
order cancellation and exchange metadata. Every signed request is HMAC-SHA256
signed with the API secret; the secret itself is never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from app.exchange.errors import BinanceAPIError, BinanceConnectivityError
from app.utils.logging_setup import get_logger

logger = get_logger("exchange.rest")


class BinanceRestClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.binance.com",
        recv_window_ms: int = 5000,
        session: Optional[aiohttp.ClientSession] = None,
        request_timeout_seconds: float = 15.0,
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._recv_window = recv_window_ms
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)
        self._server_time_offset_ms = 0

    async def __aenter__(self) -> "BinanceRestClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    def set_server_time_offset(self, offset_ms: int) -> None:
        self._server_time_offset_ms = offset_ms

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self._api_key} if self._api_key else {}

    def _sign(self, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000) + self._server_time_offset_ms
        params["recvWindow"] = self._recv_window
        query = urlencode(params, doseq=True)
        signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        signed: bool = False,
        max_retries: int = 3,
    ) -> Any:
        if self._session is None:
            await self.start()

        params = dict(params or {})
        if signed:
            params = self._sign(params)

        url = f"{self._base_url}{path}"
        attempt = 0
        backoff = 1.0
        while True:
            attempt += 1
            try:
                async with self._session.request(
                    method, url, params=params, headers=self._headers()
                ) as resp:
                    text = await resp.text()
                    data = None
                    if text:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            data = {"raw": text}

                    if resp.status >= 400:
                        code = data.get("code") if isinstance(data, dict) else None
                        message = data.get("msg") if isinstance(data, dict) else text
                        if resp.status in (418, 429) or resp.status >= 500:
                            if attempt <= max_retries:
                                retry_after = float(resp.headers.get("Retry-After", backoff))
                                logger.warning(
                                    "Binance %s %s -> HTTP %s (attempt %s/%s), retrying in %.1fs",
                                    method, path, resp.status, attempt, max_retries, retry_after,
                                )
                                await asyncio.sleep(retry_after)
                                backoff *= 2
                                continue
                        raise BinanceAPIError(resp.status, code, str(message), path)

                    return data
            except aiohttp.ClientError as exc:
                if attempt <= max_retries:
                    logger.warning(
                        "Network error calling Binance %s %s (attempt %s/%s): %s",
                        method, path, attempt, max_retries, exc,
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise BinanceConnectivityError(f"Failed to reach Binance at {path}: {exc}") from exc

    # ---------------------------------------------------------------- Public
    async def ping(self) -> None:
        await self._request("GET", "/api/v3/ping")

    async def server_time(self) -> int:
        data = await self._request("GET", "/api/v3/time")
        return int(data["serverTime"])

    async def exchange_info(self) -> dict:
        return await self._request("GET", "/api/v3/exchangeInfo")

    async def ticker_24hr_all(self) -> list:
        return await self._request("GET", "/api/v3/ticker/24hr")

    async def klines(self, symbol: str, interval: str, limit: int = 300) -> list:
        return await self._request(
            "GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )

    async def current_price(self, symbol: str) -> float:
        data = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    # ---------------------------------------------------------------- Signed
    async def account_info(self) -> dict:
        return await self._request("GET", "/api/v3/account", signed=True)

    async def open_orders(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", "/api/v3/openOrders", params, signed=True)

    async def get_order(self, symbol: str, order_id: int) -> dict:
        return await self._request(
            "GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True
        )

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        return await self._request(
            "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True
        )

    async def all_orders(self, symbol: str, limit: int = 50) -> list:
        return await self._request(
            "GET", "/api/v3/allOrders", {"symbol": symbol, "limit": limit}, signed=True
        )

    async def new_market_order_quote_qty(self, symbol: str, side: str, quote_order_qty: str) -> dict:
        """MARKET order sized by quote currency amount (spend exactly X USDT)."""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quoteOrderQty": quote_order_qty,
            "newOrderRespType": "FULL",
        }
        return await self._request("POST", "/api/v3/order", params, signed=True)

    async def new_market_order_qty(self, symbol: str, side: str, quantity: str) -> dict:
        """MARKET order sized by base asset quantity (sell exactly X coins)."""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "newOrderRespType": "FULL",
        }
        return await self._request("POST", "/api/v3/order", params, signed=True)

    async def new_limit_order(self, symbol: str, side: str, quantity: str, price: str, tif: str = "GTC") -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": tif,
            "quantity": quantity,
            "price": price,
            "newOrderRespType": "FULL",
        }
        return await self._request("POST", "/api/v3/order", params, signed=True)
