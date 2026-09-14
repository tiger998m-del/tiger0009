"""
Order Executor -- the only module allowed to talk to Binance about placing,
polling or cancelling orders. A position is NEVER considered open just
because a BUY request was sent: the resulting order is polled until it
reaches a terminal state (FILLED/CANCELED/REJECTED/EXPIRED) and the actual
executed quantity / cumulative quote quantity from Binance is what backs the
Average Entry Price and TP calculations -- never the signal price.

DRY_RUN simulation lives here, and ONLY here: when settings.dry_run is True,
no real Binance order endpoint is ever called; a deterministic simulated
fill is produced instead, clearly logged as "[DRY RUN]". This code path is
never reachable when DRY_RUN=false.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.exchange.errors import BinanceAPIError, OrderStateUncertainError
from app.exchange.rest_client import BinanceRestClient
from app.exchange.symbol_filters import SymbolFilters
from app.utils.logging_setup import get_logger

logger = get_logger("execution.orders")

_TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
_dry_run_id_counter = itertools.count(900_000_000)


@dataclass
class OrderFillResult:
    order_id: int
    status: str
    avg_price: float
    executed_qty: float
    cumulative_quote_qty: float
    fees_usdt: float


class OrderExecutor:
    def __init__(
        self,
        rest: BinanceRestClient,
        dry_run: bool,
        quote_asset: str,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 30.0,
    ):
        self._rest = rest
        self._dry_run = dry_run
        self._quote_asset = quote_asset
        self._poll_interval = poll_interval_seconds
        self._poll_timeout = poll_timeout_seconds

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _next_dry_run_id() -> int:
        return next(_dry_run_id_counter)

    def _parse_fill_response(self, resp: dict) -> OrderFillResult:
        status = resp.get("status", "UNKNOWN")
        executed_qty = float(resp.get("executedQty", 0.0))
        cumulative_quote_qty = float(resp.get("cummulativeQuoteQty", 0.0))
        avg_price = (cumulative_quote_qty / executed_qty) if executed_qty > 0 else 0.0

        fees_usdt = 0.0
        for fill in resp.get("fills", []) or []:
            if fill.get("commissionAsset") == self._quote_asset:
                fees_usdt += float(fill.get("commission", 0.0))
        # Note (documented in README "Engineering Decisions"): if commission
        # was charged in a non-quote asset (e.g. BNB), it is not converted to
        # USDT here to avoid depending on yet another live conversion rate;
        # PnL in that case is a very small overestimate. This is a deliberate,
        # documented simplification, not a silent bug.

        return OrderFillResult(
            order_id=int(resp["orderId"]),
            status=status,
            avg_price=avg_price,
            executed_qty=executed_qty,
            cumulative_quote_qty=cumulative_quote_qty,
            fees_usdt=fees_usdt,
        )

    async def _poll_until_terminal(self, symbol: str, order_id: int) -> dict:
        elapsed = 0.0
        while elapsed < self._poll_timeout:
            await asyncio.sleep(self._poll_interval)
            elapsed += self._poll_interval
            data = await self._rest.get_order(symbol, order_id)
            if data.get("status") in _TERMINAL_STATUSES:
                return data
        raise OrderStateUncertainError(
            f"Order {order_id} on {symbol} did not reach a terminal state within "
            f"{self._poll_timeout}s. Do NOT assume fill/no-fill -- reconcile manually "
            f"via recovery before taking further action on this symbol."
        )

    # --------------------------------------------------------------- BUY
    async def market_buy(
        self, symbol: str, quote_qty: Decimal, reference_price: Decimal, filters: SymbolFilters
    ) -> OrderFillResult:
        if self._dry_run:
            qty = filters.round_qty(quote_qty / reference_price, market_order=True)
            fill = OrderFillResult(
                order_id=self._next_dry_run_id(),
                status="FILLED",
                avg_price=float(reference_price),
                executed_qty=float(qty),
                cumulative_quote_qty=float(qty * reference_price),
                fees_usdt=0.0,
            )
            logger.info("[DRY RUN] simulated MARKET BUY %s qty=%s price=%s", symbol, qty, reference_price)
            return fill

        resp = await self._rest.new_market_order_quote_qty(symbol, "BUY", str(quote_qty))
        if resp.get("status") not in _TERMINAL_STATUSES:
            resp = await self._poll_until_terminal(symbol, int(resp["orderId"]))
        result = self._parse_fill_response(resp)
        logger.info(
            "MARKET BUY executed: %s order_id=%s status=%s avg_price=%s qty=%s",
            symbol, result.order_id, result.status, result.avg_price, result.executed_qty,
        )
        return result

    # ------------------------------------------------------------ TP LIMIT
    async def place_tp_limit_sell(
        self, symbol: str, qty: Decimal, price: Decimal, filters: SymbolFilters
    ) -> tuple[int, str]:
        if self._dry_run:
            order_id = self._next_dry_run_id()
            logger.info("[DRY RUN] simulated LIMIT SELL (TP) placed %s qty=%s price=%s", symbol, qty, price)
            return order_id, "NEW"

        resp = await self._rest.new_limit_order(symbol, "SELL", str(qty), str(price))
        order_id = int(resp["orderId"])
        status = resp.get("status", "NEW")
        logger.info("TP LIMIT SELL placed: %s order_id=%s status=%s price=%s qty=%s",
                     symbol, order_id, status, price, qty)
        return order_id, status

    # --------------------------------------------------------------- SELL
    async def market_sell(
        self, symbol: str, qty: Decimal, reference_price: Decimal, filters: SymbolFilters
    ) -> OrderFillResult:
        if self._dry_run:
            fill = OrderFillResult(
                order_id=self._next_dry_run_id(),
                status="FILLED",
                avg_price=float(reference_price),
                executed_qty=float(qty),
                cumulative_quote_qty=float(qty * reference_price),
                fees_usdt=0.0,
            )
            logger.info("[DRY RUN] simulated MARKET SELL %s qty=%s price=%s", symbol, qty, reference_price)
            return fill

        resp = await self._rest.new_market_order_qty(symbol, "SELL", str(qty))
        if resp.get("status") not in _TERMINAL_STATUSES:
            resp = await self._poll_until_terminal(symbol, int(resp["orderId"]))
        result = self._parse_fill_response(resp)
        logger.info(
            "MARKET SELL executed: %s order_id=%s status=%s avg_price=%s qty=%s",
            symbol, result.order_id, result.status, result.avg_price, result.executed_qty,
        )
        return result

    # ------------------------------------------------------------- CANCEL
    async def cancel_order_safe(self, symbol: str, order_id: int) -> dict:
        """Cancels an order, tolerating the case where it was already
        filled/cancelled on Binance's side (error code -2011 'Unknown order
        sent'). Always returns the order's actual final state -- callers
        MUST check this to avoid a double sell."""
        if self._dry_run:
            return {"status": "CANCELED", "orderId": order_id}

        try:
            resp = await self._rest.cancel_order(symbol, order_id)
            return resp
        except BinanceAPIError as exc:
            if exc.code == -2011:
                # Order no longer open -- find out whether it was FILLED first.
                status = await self._rest.get_order(symbol, order_id)
                logger.warning(
                    "Cancel on %s order_id=%s got 'Unknown order' (-2011); actual status=%s",
                    symbol, order_id, status.get("status"),
                )
                return status
            raise

    async def get_order_status(self, symbol: str, order_id: int) -> dict:
        if self._dry_run:
            # Simulated TP orders are resolved directly by PositionManager
            # comparing live price to the TP target rather than by polling
            # (see position_manager.on_price_tick), so this path is not
            # exercised in DRY_RUN mode.
            return {"status": "NEW", "orderId": order_id}
        return await self._rest.get_order(symbol, order_id)
