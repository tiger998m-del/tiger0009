"""
Position Manager -- owns the full lifecycle of a trade from signal
acceptance to close, and is the single place where the following
protections live:

  * Entry Lock / Duplicate Entry Protection (constitution #17): an
    `asyncio.Lock` per symbol plus an "entry in progress" guard set ensures
    that even if the same BUY signal arrives twice (WebSocket replay,
    reconnect, delayed processing) only one BUY order is ever sent.

  * Position Exit Lock / Double Sell Protection (constitution #18): an
    `asyncio.Lock` per open position guards the OPEN -> CLOSING transition.
    Whichever exit trigger (ATR Stop, EMA8 Exit, Take Profit fill) wins the
    race is the only one allowed to proceed; everything else observes
    status != OPEN and backs off. The TP limit order and the ATR/EMA8 market
    sell are mutually cancelling by construction: closing via one path
    always cancels (or verifies) the other order first, which is exactly
    "a TP order and a SL mechanism where triggering one cancels the other"
    from the general constitution, implemented with three exit sources
    instead of a plain two-leg OCO because EMA8-break is not expressible as
    a native Binance OCO leg.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional, Set

import pandas as pd

from app.config.settings import Settings
from app.database.models import STATUS_CLOSED, STATUS_CLOSING, STATUS_OPEN, STATUS_REJECTED, Trade
from app.database.repository import TradeRepository
from app.exchange.errors import OrderStateUncertainError
from app.exchange.rest_client import BinanceRestClient
from app.exchange.symbol_filters import SymbolFilters
from app.execution.order_executor import OrderExecutor
from app.notifications.telegram_notifier import NotificationMessages, TelegramNotifier
from app.risk.risk_manager import RiskManager
from app.strategy.entry_signal import EntrySignalResult
from app.strategy.signal_engine import SignalEngine
from app.utils.logging_setup import get_logger
from app.utils.time_utils import utcnow

logger = get_logger("execution.position_manager")


@dataclass
class RuntimePosition:
    trade_id: int
    symbol: str
    qty: Decimal
    entry_price: Decimal
    tp_price: Decimal
    active_atr_stop: Decimal
    tp_order_id: Optional[int]
    filters: SymbolFilters
    status: str = STATUS_OPEN
    exit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tp_poll_task: Optional[asyncio.Task] = None


class PositionManager:
    def __init__(
        self,
        settings: Settings,
        repo: TradeRepository,
        risk_manager: RiskManager,
        order_executor: OrderExecutor,
        rest: BinanceRestClient,
        notifier: TelegramNotifier,
        signal_engine: SignalEngine,
        filters_map: Dict[str, SymbolFilters],
    ):
        self._settings = settings
        self._repo = repo
        self._risk = risk_manager
        self._executor = order_executor
        self._rest = rest
        self._notifier = notifier
        self._signal_engine = signal_engine
        self._filters_map = filters_map

        self._open: Dict[str, RuntimePosition] = {}
        self._entry_locks: Dict[str, asyncio.Lock] = {}
        self._entry_in_progress: Set[str] = set()

    # ------------------------------------------------------------- queries
    def open_symbols(self) -> list[str]:
        return list(self._open.keys())

    def open_count(self) -> int:
        return len(self._open)

    def has_open_or_pending(self, symbol: str) -> bool:
        return symbol in self._open or symbol in self._entry_in_progress

    def _lock_for(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._entry_locks:
            self._entry_locks[symbol] = asyncio.Lock()
        return self._entry_locks[symbol]

    async def _available_quote_balance(self) -> float:
        if self._settings.dry_run:
            # In DRY_RUN there is no real balance to query safely (API keys
            # may not even be configured). We simulate an account that always
            # has exactly the configured bot capital available, which is the
            # safest, most predictable behaviour for a paper-trading mode.
            used = sum(float(p.qty) * float(p.entry_price) for p in self._open.values())
            return max(self._settings.capital_usdt - used, 0.0)

        account = await self._rest.account_info()
        for bal in account.get("balances", []):
            if bal.get("asset") == self._settings.quote_asset:
                return float(bal.get("free", 0.0))
        return 0.0

    # --------------------------------------------------------------- entry
    async def try_enter(self, symbol: str, entry_result: EntrySignalResult) -> bool:
        if self.has_open_or_pending(symbol):
            logger.info("Duplicate entry prevented for %s: already open or entry in progress", symbol)
            return False

        lock = self._lock_for(symbol)
        if lock.locked():
            logger.info("Duplicate entry prevented for %s: entry lock busy", symbol)
            return False

        async with lock:
            if self.has_open_or_pending(symbol):
                return False
            self._entry_in_progress.add(symbol)
            try:
                return await self._execute_entry(symbol, entry_result)
            finally:
                self._entry_in_progress.discard(symbol)

    async def _execute_entry(self, symbol: str, entry_result: EntrySignalResult) -> bool:
        allowed, reason = self._risk.can_open_new_trade(self.open_count())
        if not allowed:
            if "Max open positions" in reason:
                await self._notifier.send(NotificationMessages.max_positions_reached(symbol))
            logger.info("Entry blocked for %s: %s", symbol, reason)
            return False

        filters = self._filters_map.get(symbol)
        if filters is None:
            logger.warning("No symbol filters cached for %s; skipping entry", symbol)
            return False

        try:
            balance = await self._available_quote_balance()
        except Exception:
            logger.exception("Failed to fetch account balance before BUY on %s; aborting entry", symbol)
            await self._notifier.send(NotificationMessages.api_error("balance check before BUY", "see logs"))
            return False

        capital, reduced = self._risk.compute_trade_capital(balance)
        if capital <= 0:
            logger.info("Entry skipped for %s: no available balance", symbol)
            return False
        if reduced:
            logger.warning(
                "Position size for %s reduced from theoretical %.2f USDT to actual available %.2f USDT",
                symbol, self._risk.per_trade_usdt, capital,
            )

        close_price = Decimal(str(entry_result.close))
        quote_qty = Decimal(str(round(capital, 8)))
        estimated_qty = filters.round_qty(quote_qty / close_price, market_order=True)
        ok, filt_reason = filters.validate_order(close_price, estimated_qty, market_order=True)
        if not ok:
            logger.info("Entry rejected for %s by Binance filters: %s", symbol, filt_reason)
            await self._notifier.send(NotificationMessages.order_rejected_by_filters(symbol, filt_reason))
            return False

        trade_id = self._repo.create_trade(Trade(symbol=symbol, capital_used_usdt=capital))
        await self._notifier.send(NotificationMessages.buy_order_sent(symbol, capital))

        try:
            fill = await self._executor.market_buy(symbol, quote_qty, close_price, filters)
        except OrderStateUncertainError as exc:
            logger.error("BUY order state uncertain for %s: %s", symbol, exc)
            self._repo.update_trade(trade_id, status="NEEDS_MANUAL_REVIEW")
            await self._notifier.send(NotificationMessages.state_mismatch(symbol, str(exc)))
            return False
        except Exception as exc:
            logger.exception("BUY order failed for %s", symbol)
            self._repo.update_trade(trade_id, status=STATUS_REJECTED)
            await self._notifier.send(NotificationMessages.api_error(f"BUY {symbol}", str(exc)))
            return False

        if fill.executed_qty <= 0 or fill.status not in ("FILLED",):
            logger.warning("BUY order for %s did not result in a fill (status=%s)", symbol, fill.status)
            self._repo.update_trade(trade_id, status=STATUS_REJECTED, buy_order_id=fill.order_id)
            await self._notifier.send(NotificationMessages.order_rejected_by_filters(
                symbol, f"BUY order ended with status {fill.status}"
            ))
            return False

        entry_price = Decimal(str(fill.avg_price))
        qty = Decimal(str(fill.executed_qty))
        tp_price = filters.round_price(entry_price * Decimal(str(1 + self._settings.take_profit_pct)))
        active_atr_stop = filters.round_price(Decimal(str(entry_result.initial_sl)))

        try:
            tp_order_id, _tp_status = await self._executor.place_tp_limit_sell(symbol, qty, tp_price, filters)
        except Exception as exc:
            logger.exception("Failed to place TP limit order for %s after BUY filled; position is UNPROTECTED", symbol)
            tp_order_id = None
            await self._notifier.send(NotificationMessages.api_error(
                f"TP placement {symbol}", f"BUY filled but TP order failed: {exc}. Manual attention required."
            ))

        now = utcnow().isoformat()
        self._repo.update_trade(
            trade_id,
            buy_order_id=fill.order_id,
            tp_order_id=tp_order_id,
            entry_time=now,
            avg_entry_price=float(entry_price),
            executed_qty=float(qty),
            tp_price=float(tp_price),
            initial_atr_stop=float(active_atr_stop),
            active_atr_stop=float(active_atr_stop),
            best_stop=float(active_atr_stop),
            status=STATUS_OPEN,
            fees_usdt=fill.fees_usdt,
        )

        position = RuntimePosition(
            trade_id=trade_id,
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            tp_price=tp_price,
            active_atr_stop=active_atr_stop,
            tp_order_id=tp_order_id,
            filters=filters,
            status=STATUS_OPEN,
        )
        self._open[symbol] = position
        if tp_order_id is not None and not self._settings.dry_run:
            position.tp_poll_task = asyncio.create_task(self._poll_tp_fill(position))

        await self._notifier.send(NotificationMessages.buy_executed(
            symbol, float(entry_price), float(qty), capital, float(tp_price), float(active_atr_stop)
        ))
        logger.info("Position opened: %s qty=%s entry=%s tp=%s atr_stop=%s",
                     symbol, qty, entry_price, tp_price, active_atr_stop)
        return True

    # ------------------------------------------------------------ recovery
    def restore_position(self, trade: Trade, filters: SymbolFilters) -> RuntimePosition:
        position = RuntimePosition(
            trade_id=trade.id,
            symbol=trade.symbol,
            qty=Decimal(str(trade.executed_qty)),
            entry_price=Decimal(str(trade.avg_entry_price)),
            tp_price=Decimal(str(trade.tp_price)),
            active_atr_stop=Decimal(str(trade.active_atr_stop)),
            tp_order_id=trade.tp_order_id,
            filters=filters,
            status=STATUS_OPEN,
        )
        self._open[trade.symbol] = position
        if position.tp_order_id is not None and not self._settings.dry_run:
            position.tp_poll_task = asyncio.create_task(self._poll_tp_fill(position))
        logger.info("Position restored from database: %s (trade_id=%s)", trade.symbol, trade.id)
        return position

    # ------------------------------------------------------------- TP poll
    async def _poll_tp_fill(self, position: RuntimePosition) -> None:
        try:
            while position.status == STATUS_OPEN:
                await asyncio.sleep(self._settings.tp_poll_interval_seconds)
                if position.status != STATUS_OPEN:
                    return
                try:
                    status = await self._executor.get_order_status(position.symbol, position.tp_order_id)
                except Exception:
                    logger.exception("Failed polling TP order status for %s", position.symbol)
                    continue
                state = status.get("status")
                if state == "FILLED":
                    await self.request_exit(position.symbol, "TAKE_PROFIT", already_filled_order=status)
                    return
                if state in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.warning(
                        "TP order for %s is %s outside of our own cancel flow; position remains monitored "
                        "by ATR stop / EMA8 exit only until manually reviewed.", position.symbol, state,
                    )
                    await self._notifier.send(NotificationMessages.api_error(
                        f"TP order {position.symbol}", f"TP order unexpectedly {state}"
                    ))
                    return
        except asyncio.CancelledError:
            return

    # -------------------------------------------------------- live triggers
    async def on_price_tick(self, symbol: str, price: float) -> None:
        position = self._open.get(symbol)
        if not position or position.status != STATUS_OPEN:
            return
        price_d = Decimal(str(price))
        if price_d <= position.active_atr_stop:
            await self.request_exit(symbol, "ATR_STOP", trigger_price=price_d)
            return
        if self._settings.dry_run and price_d >= position.tp_price:
            await self.request_exit(symbol, "TAKE_PROFIT", trigger_price=price_d)

    async def on_candle_closed(self, symbol: str, df_closed: pd.DataFrame) -> None:
        position = self._open.get(symbol)
        if not position or position.status != STATUS_OPEN:
            return

        evaluation = self._signal_engine.evaluate_exit(df_closed, float(position.active_atr_stop))
        new_stop = position.filters.round_price(evaluation.new_active_sl)
        if new_stop > position.active_atr_stop:
            position.active_atr_stop = new_stop
            self._repo.update_trade(
                position.trade_id, active_atr_stop=float(new_stop), best_stop=float(new_stop)
            )
            await self._notifier.send(NotificationMessages.atr_stop_updated(symbol, float(new_stop)))
            logger.info("ATR stop updated for %s -> %s", symbol, new_stop)

        if evaluation.ema8_exit.triggered:
            logger.info("EMA8 breakdown exit triggered for %s: %s", symbol, evaluation.ema8_exit.reason)
            await self.request_exit(symbol, "EMA8_EXIT")

    # ----------------------------------------------------------------- exit
    async def request_exit(
        self,
        symbol: str,
        reason: str,
        trigger_price: Optional[Decimal] = None,
        already_filled_order: Optional[dict] = None,
    ) -> None:
        position = self._open.get(symbol)
        if not position:
            return

        async with position.exit_lock:
            if position.status != STATUS_OPEN:
                logger.info(
                    "Exit request '%s' for %s ignored: position already %s (double-sell prevented)",
                    reason, symbol, position.status,
                )
                return
            position.status = STATUS_CLOSING
            self._repo.update_trade(position.trade_id, status=STATUS_CLOSING, exit_reason=reason)

        try:
            fill_price, exit_qty, final_reason = await self._perform_exit(
                position, reason, trigger_price, already_filled_order
            )
        except Exception:
            logger.exception(
                "Exit execution FAILED for %s (reason=%s). Position left in CLOSING state and "
                "excluded from further automated action -- manual review required "
                "(capital protection takes priority over automated recovery).", symbol, reason,
            )
            self._repo.update_trade(position.trade_id, status="NEEDS_MANUAL_REVIEW")
            await self._notifier.send(NotificationMessages.state_mismatch(
                symbol, f"Exit ({reason}) failed to confirm; trade left CLOSING/NEEDS_MANUAL_REVIEW"
            ))
            return

        pnl = (fill_price - float(position.entry_price)) * float(exit_qty)
        position.status = STATUS_CLOSED
        self._repo.update_trade(
            position.trade_id,
            status=STATUS_CLOSED,
            exit_price=fill_price,
            exit_time=utcnow().isoformat(),
            exit_reason=final_reason,
            pnl_usdt=pnl,
        )

        if position.tp_poll_task:
            position.tp_poll_task.cancel()
        self._open.pop(symbol, None)

        if final_reason == "TAKE_PROFIT":
            await self._notifier.send(NotificationMessages.take_profit_executed(symbol, fill_price, pnl))
        elif final_reason == "ATR_STOP":
            await self._notifier.send(NotificationMessages.stop_loss_executed(symbol, fill_price, pnl))
        else:
            await self._notifier.send(NotificationMessages.exit_executed(symbol, final_reason, fill_price, pnl))

        logger.info("Position closed: %s reason=%s exit_price=%s pnl=%.4f USDT", symbol, final_reason, fill_price, pnl)

        for event in self._risk.register_closed_trade(pnl):
            if event.kind == "DAILY_STOP":
                await self._notifier.send(NotificationMessages.daily_stop_triggered(event.message))
            elif event.kind == "PAUSE_TRIGGERED":
                await self._notifier.send(NotificationMessages.pause_triggered(event.message))

    async def _perform_exit(
        self,
        position: RuntimePosition,
        reason: str,
        trigger_price: Optional[Decimal],
        already_filled_order: Optional[dict],
    ) -> tuple[float, Decimal, str]:
        symbol = position.symbol

        if reason == "TAKE_PROFIT":
            if already_filled_order:
                executed_qty = float(already_filled_order.get("executedQty", position.qty))
                cumulative_quote = float(already_filled_order.get("cummulativeQuoteQty", 0.0))
                fill_price = (cumulative_quote / executed_qty) if executed_qty else float(position.tp_price)
                return fill_price, Decimal(str(executed_qty)), "TAKE_PROFIT"
            # DRY_RUN instantaneous TP hit via price tick
            return float(position.tp_price), position.qty, "TAKE_PROFIT"

        # ATR_STOP or EMA8_EXIT: cancel the TP order first (this is the
        # "executing one cancels the other" mechanism), but verify it was not
        # already filled in the same instant -- if it was, defer to TAKE_PROFIT
        # instead of also placing a market sell (double-sell protection).
        if position.tp_order_id is not None:
            cancel_result = await self._executor.cancel_order_safe(symbol, position.tp_order_id)
            if cancel_result.get("status") == "FILLED":
                executed_qty = float(cancel_result.get("executedQty", position.qty))
                cumulative_quote = float(cancel_result.get("cummulativeQuoteQty", 0.0))
                fill_price = (cumulative_quote / executed_qty) if executed_qty else float(position.tp_price)
                return fill_price, Decimal(str(executed_qty)), "TAKE_PROFIT"

        sell_reference_price = trigger_price if trigger_price is not None else position.active_atr_stop
        sell_fill = await self._executor.market_sell(symbol, position.qty, sell_reference_price, position.filters)
        return sell_fill.avg_price, Decimal(str(sell_fill.executed_qty)), reason
