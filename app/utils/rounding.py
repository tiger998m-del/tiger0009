"""
Decimal-precision rounding helpers used to make prices/quantities comply with
Binance's exchange filters (tickSize / stepSize). Using Decimal instead of
float avoids the classic 0.1 + 0.2 != 0.3 style errors that would otherwise
cause spurious LOT_SIZE / PRICE_FILTER rejections.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def step_precision(step: Decimal) -> int:
    """Number of decimal places implied by a step/tick size, e.g. 0.0010 -> 3."""
    step = step.normalize()
    exponent = step.as_tuple().exponent
    return max(0, -exponent) if exponent < 0 else 0


def floor_to_step(value, step) -> Decimal:
    """Round `value` DOWN to the nearest multiple of `step` (never rounds up,
    so we never place an order Binance would reject as exceeding balance)."""
    value = to_decimal(value)
    step = to_decimal(step)
    if step == 0:
        return value
    quotient = (value / step).to_integral_value(rounding=ROUND_DOWN)
    result = quotient * step
    precision = step_precision(step)
    quant = Decimal(1).scaleb(-precision) if precision > 0 else Decimal(1)
    return result.quantize(quant, rounding=ROUND_DOWN)


def round_half_up_to_step(value, step) -> Decimal:
    """Round to the nearest multiple of `step` using conventional rounding.
    Used for display / TP target computation, never for order quantities."""
    value = to_decimal(value)
    step = to_decimal(step)
    if step == 0:
        return value
    quotient = (value / step).to_integral_value(rounding=ROUND_HALF_UP)
    result = quotient * step
    precision = step_precision(step)
    quant = Decimal(1).scaleb(-precision) if precision > 0 else Decimal(1)
    return result.quantize(quant, rounding=ROUND_HALF_UP)
