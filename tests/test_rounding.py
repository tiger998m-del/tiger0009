from __future__ import annotations

from decimal import Decimal

from app.exchange.symbol_filters import SymbolFilters
from app.utils.rounding import floor_to_step, round_half_up_to_step, step_precision


def test_floor_to_step_never_rounds_up():
    # step 0.001, value 1.2349 -> must floor to 1.234, never 1.235
    result = floor_to_step("1.2349", "0.001")
    assert result == Decimal("1.234")


def test_floor_to_step_exact_multiple_unchanged():
    result = floor_to_step("2.500", "0.5")
    assert result == Decimal("2.5")


def test_step_precision_detects_decimal_places():
    assert step_precision(Decimal("0.00100000")) == 3
    assert step_precision(Decimal("1")) == 0


def test_round_half_up_to_step():
    result = round_half_up_to_step("1.2355", "0.001")
    assert result == Decimal("1.236")


def _make_symbol_filters(**overrides) -> SymbolFilters:
    sym = {
        "symbol": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "isSpotTradingAllowed": True,
        "baseAssetPrecision": 8,
        "quoteAssetPrecision": 8,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00001", "maxQty": "3200", "stepSize": "0.00001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "10.00", "applyToMarket": True},
        ],
    }
    sym.update(overrides)
    return SymbolFilters.from_exchange_info_symbol(sym)


def test_symbol_filters_round_price_to_tick_size():
    f = _make_symbol_filters()
    assert f.round_price("27123.456") == Decimal("27123.45")


def test_symbol_filters_round_qty_to_step_size():
    f = _make_symbol_filters()
    assert f.round_qty("0.123456789") == Decimal("0.12345")


def test_symbol_filters_rejects_below_min_notional():
    f = _make_symbol_filters()
    ok, reason = f.validate_order(price="1.0", qty="1.0", market_order=True)
    assert not ok
    assert "MIN_NOTIONAL" in reason


def test_symbol_filters_rejects_below_lot_size_min_qty():
    f = _make_symbol_filters()
    ok, reason = f.validate_order(price="100000", qty="0.000001", market_order=True)
    assert not ok
    assert "LOT_SIZE" in reason


def test_symbol_filters_accepts_valid_order():
    f = _make_symbol_filters()
    ok, reason = f.validate_order(price="100", qty="1", market_order=True)
    assert ok, reason


def test_symbol_filters_rejects_non_trading_status():
    f = _make_symbol_filters(status="BREAK")
    ok, reason = f.validate_order(price="100", qty="1", market_order=True)
    assert not ok
    assert "TRADING" in reason
