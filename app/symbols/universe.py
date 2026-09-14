"""
Builds the tradable symbol universe according to constitution section 1:
  - USDT Spot pair
  - Market Cap >= MIN_MARKET_CAP_USD
  - 24h Volume >= MIN_VOLUME_24H_USD

Leveraged tokens (e.g. BTCUP, BTCDOWN, BULL/BEAR) are excluded on purpose:
they are not genuine spot long exposure to the underlying asset and would
violate the "no leverage" requirement in spirit even though Binance lists
them as ordinary SPOT symbols. This is a documented engineering decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from app.config.settings import Settings
from app.exchange.rest_client import BinanceRestClient
from app.exchange.symbol_filters import SymbolFilters
from app.symbols.market_cap_provider import MarketCapProvider
from app.utils.logging_setup import get_logger

logger = get_logger("symbols.universe")

_LEVERAGED_TOKEN_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def _is_leveraged_token(base_asset: str) -> bool:
    return base_asset.endswith(_LEVERAGED_TOKEN_SUFFIXES) and len(base_asset) > 4


@dataclass
class UniverseSymbol:
    symbol: str
    base_asset: str
    quote_volume_24h: float
    market_cap_usd: float
    filters: SymbolFilters


async def load_all_symbol_filters(rest: BinanceRestClient) -> Dict[str, SymbolFilters]:
    info = await rest.exchange_info()
    result: Dict[str, SymbolFilters] = {}
    for sym in info.get("symbols", []):
        try:
            result[sym["symbol"]] = SymbolFilters.from_exchange_info_symbol(sym)
        except Exception as exc:
            logger.debug("Skipping malformed symbol info for %s: %s", sym.get("symbol"), exc)
    return result


async def build_universe(
    rest: BinanceRestClient,
    market_cap_provider: MarketCapProvider,
    settings: Settings,
    symbol_filters: Dict[str, SymbolFilters],
) -> List[UniverseSymbol]:
    tickers = await rest.ticker_24hr_all()
    candidates: List[UniverseSymbol] = []

    for t in tickers:
        symbol = t.get("symbol", "")
        filters = symbol_filters.get(symbol)
        if filters is None:
            continue
        if filters.quote_asset != settings.quote_asset:
            continue
        tradable, _reason = filters.is_tradable()
        if not tradable:
            continue
        if _is_leveraged_token(filters.base_asset):
            continue

        try:
            quote_volume = float(t.get("quoteVolume", 0.0))
        except (TypeError, ValueError):
            continue

        if quote_volume < settings.min_volume_24h_usd:
            continue

        market_cap = 0.0
        if settings.market_cap_check_enabled:
            mc = await market_cap_provider.get_market_cap_usd(filters.base_asset)
            if mc is None:
                logger.info("Rejecting %s from universe: market cap unknown/unverifiable", symbol)
                continue
            market_cap = mc
            if market_cap < settings.min_market_cap_usd:
                continue
        candidates.append(
            UniverseSymbol(
                symbol=symbol,
                base_asset=filters.base_asset,
                quote_volume_24h=quote_volume,
                market_cap_usd=market_cap,
                filters=filters,
            )
        )

    candidates.sort(key=lambda c: c.quote_volume_24h, reverse=True)
    limited = candidates[: settings.universe_max_symbols]
    logger.info(
        "Universe built: %d symbols passed screening, %d kept after UNIVERSE_MAX_SYMBOLS cap",
        len(candidates), len(limited),
    )
    return limited
