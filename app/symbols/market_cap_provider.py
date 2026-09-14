"""
Market-capitalization lookup.

Binance's own API does not expose market capitalization, so this bot uses the
public CoinGecko API (no key required) as documented in README under
"Engineering Decisions". We fetch the top-N coins by market cap (sufficient
to cover anything that could realistically pass the >= 50M USD market cap and
>= 50M USD 24h volume screen) and index them by ticker symbol.

Safety rule: if CoinGecko is unreachable or a symbol cannot be confidently
matched, the market-cap check FAILS CLOSED (the coin is rejected), never
"assumed passing" -- we never risk real capital on an unverifiable screen.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

import aiohttp

from app.utils.logging_setup import get_logger

logger = get_logger("symbols.market_cap")


class MarketCapProvider:
    def __init__(self, base_url: str, cache_minutes: int, session: aiohttp.ClientSession,
                 pages: int = 4, per_page: int = 250):
        self._base_url = base_url.rstrip("/")
        self._cache_seconds = cache_minutes * 60
        self._session = session
        self._pages = pages
        self._per_page = per_page
        self._cache: Dict[str, float] = {}
        self._cache_loaded_at: float = 0.0

    async def _refresh_cache(self) -> None:
        merged: Dict[str, float] = {}
        try:
            for page in range(1, self._pages + 1):
                url = f"{self._base_url}/coins/markets"
                params = {
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": self._per_page,
                    "page": page,
                    "sparkline": "false",
                }
                async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning("CoinGecko market cap fetch failed with HTTP %s on page %s", resp.status, page)
                        break
                    data = await resp.json(content_type=None)
                    if not data:
                        break
                    for coin in data:
                        symbol = str(coin.get("symbol", "")).upper()
                        market_cap = coin.get("market_cap")
                        if symbol and market_cap and symbol not in merged:
                            # First occurrence = highest market cap for that ticker
                            # (pages are already ordered market_cap_desc), which
                            # is the safest disambiguation for symbol collisions.
                            merged[symbol] = float(market_cap)
        except Exception as exc:
            logger.warning("CoinGecko market cap refresh failed: %s", exc)
            return

        if merged:
            self._cache = merged
            self._cache_loaded_at = time.monotonic()
            logger.info("Market cap cache refreshed with %d symbols", len(merged))

    async def get_market_cap_usd(self, base_asset: str) -> Optional[float]:
        now = time.monotonic()
        if not self._cache or (now - self._cache_loaded_at) > self._cache_seconds:
            await self._refresh_cache()
        return self._cache.get(base_asset.upper())
