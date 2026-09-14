"""
Parses Binance `exchangeInfo` per-symbol filters and provides safe rounding /
validation of price & quantity before any order is ever sent, per
constitution item #15 (Binance Filters).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.utils.rounding import floor_to_step, to_decimal


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    is_spot_trading_allowed: bool
    base_asset_precision: int
    quote_asset_precision: int
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Optional[Decimal]
    market_min_qty: Optional[Decimal]
    market_max_qty: Optional[Decimal]
    min_notional: Decimal
    apply_min_notional_to_market: bool

    @classmethod
    def from_exchange_info_symbol(cls, sym: dict) -> "SymbolFilters":
        filters = {f["filterType"]: f for f in sym.get("filters", [])}

        price_filter = filters.get("PRICE_FILTER", {})
        lot_size = filters.get("LOT_SIZE", {})
        market_lot_size = filters.get("MARKET_LOT_SIZE", {})
        min_notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}

        apply_to_market = bool(min_notional_filter.get("applyToMarket", True)) if "MIN_NOTIONAL" in filters else bool(
            min_notional_filter.get("applyMinNotionalToMarket", True)
        )

        return cls(
            symbol=sym["symbol"],
            base_asset=sym["baseAsset"],
            quote_asset=sym["quoteAsset"],
            status=sym.get("status", "UNKNOWN"),
            is_spot_trading_allowed=bool(sym.get("isSpotTradingAllowed", False)),
            base_asset_precision=int(sym.get("baseAssetPrecision", 8)),
            quote_asset_precision=int(sym.get("quoteAssetPrecision", 8)),
            tick_size=to_decimal(price_filter.get("tickSize", "0.00000001")),
            min_price=to_decimal(price_filter.get("minPrice", "0")),
            max_price=to_decimal(price_filter.get("maxPrice", "1000000000")),
            step_size=to_decimal(lot_size.get("stepSize", "0.00000001")),
            min_qty=to_decimal(lot_size.get("minQty", "0")),
            max_qty=to_decimal(lot_size.get("maxQty", "9000000000")),
            market_step_size=to_decimal(market_lot_size["stepSize"]) if market_lot_size.get("stepSize") else None,
            market_min_qty=to_decimal(market_lot_size["minQty"]) if market_lot_size.get("minQty") else None,
            market_max_qty=to_decimal(market_lot_size["maxQty"]) if market_lot_size.get("maxQty") else None,
            min_notional=to_decimal(
                min_notional_filter.get("minNotional") or min_notional_filter.get("notional") or "0"
            ),
            apply_min_notional_to_market=apply_to_market,
        )

    def is_tradable(self) -> tuple[bool, str]:
        if self.status != "TRADING":
            return False, f"symbol status is {self.status}, not TRADING"
        if not self.is_spot_trading_allowed:
            return False, "symbol does not allow SPOT trading"
        if self.quote_asset != "USDT":
            return False, f"quote asset is {self.quote_asset}, not USDT"
        return True, ""

    def round_price(self, price) -> Decimal:
        return floor_to_step(price, self.tick_size)

    def round_qty(self, qty, market_order: bool = True) -> Decimal:
        step = self.market_step_size if (market_order and self.market_step_size) else self.step_size
        return floor_to_step(qty, step)

    def validate_order(self, price, qty, market_order: bool = True) -> tuple[bool, str]:
        ok, reason = self.is_tradable()
        if not ok:
            return False, reason

        price_d = to_decimal(price)
        qty_d = to_decimal(qty)

        min_qty = self.market_min_qty if (market_order and self.market_min_qty is not None) else self.min_qty
        max_qty = self.market_max_qty if (market_order and self.market_max_qty is not None) else self.max_qty

        if qty_d < min_qty:
            return False, f"LOT_SIZE: quantity {qty_d} below minQty {min_qty}"
        if qty_d > max_qty:
            return False, f"LOT_SIZE: quantity {qty_d} above maxQty {max_qty}"

        if not market_order:
            if price_d < self.min_price and self.min_price > 0:
                return False, f"PRICE_FILTER: price {price_d} below minPrice {self.min_price}"
            if price_d > self.max_price and self.max_price > 0:
                return False, f"PRICE_FILTER: price {price_d} above maxPrice {self.max_price}"

        if self.min_notional > 0:
            if market_order and not self.apply_min_notional_to_market:
                pass
            else:
                notional = price_d * qty_d
                if notional < self.min_notional:
                    return False, f"MIN_NOTIONAL: notional {notional} below minNotional {self.min_notional}"

        return True, ""
