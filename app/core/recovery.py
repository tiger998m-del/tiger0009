"""
Startup Recovery (constitution #20).

On every startup, BEFORE any new-entry scanning is allowed, the bot:
  1. Reads open trades (PENDING_ENTRY / OPEN / CLOSING) from SQLite.
  2. Reads live Binance account balances and order status for those trades.
  3. Reconciles local state with Binance reality.
  4. Restores cleanly-consistent OPEN positions (TP + ATR stop + monitoring).
  5. For anything ambiguous or inconsistent, NEVER guesses: it marks the
     trade NEEDS_MANUAL_REVIEW, sends an urgent Telegram alert, and leaves
     that symbol out of both position monitoring and new-entry scanning
     until a human resolves it. Capital protection takes priority over
     automatically resuming trading (constitution #29).

In DRY_RUN mode there is no real exchange truth to reconcile against (no
real orders were ever placed), so any open trades left over from a previous
DRY_RUN session are restored directly from the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set

from app.config.settings import Settings
from app.database.models import STATUS_CLOSED, STATUS_NEEDS_MANUAL_REVIEW, STATUS_REJECTED, Trade
from app.database.repository import TradeRepository
from app.exchange.rest_client import BinanceRestClient
from app.exchange.symbol_filters import SymbolFilters
from app.execution.position_manager import PositionManager
from app.notifications.telegram_notifier import NotificationMessages, TelegramNotifier
from app.risk.risk_manager import RiskManager
from app.utils.logging_setup import get_logger
from app.utils.time_utils import utcnow

logger = get_logger("core.recovery")

_CONSISTENCY_TOLERANCE = 0.01  # allow 1% slack for rounding/dust


@dataclass
class RecoveryReport:
    restored: int = 0
    mismatches: int = 0


async def _held_quantity(rest: BinanceRestClient, base_asset: str) -> float:
    account = await rest.account_info()
    for b in account.get("balances", []):
        if b.get("asset") == base_asset:
            return float(b.get("free", 0.0)) + float(b.get("locked", 0.0))
    return 0.0


async def run_recovery(
    settings: Settings,
    rest: BinanceRestClient,
    repo: TradeRepository,
    position_manager: PositionManager,
    risk_manager: RiskManager,
    notifier: TelegramNotifier,
    filters_map: Dict[str, SymbolFilters],
) -> RecoveryReport:
    open_trades = repo.get_open_trades()
    if not open_trades:
        logger.info("Recovery: no open trades found in database; starting clean.")
        return RecoveryReport(0, 0)

    logger.info("Recovery: found %d open trade(s) in database, reconciling with Binance...", len(open_trades))
    await notifier.send(NotificationMessages.recovery_started())

    report = RecoveryReport()
    blocked_symbols: Set[str] = set()

    for trade in open_trades:
        try:
            if settings.dry_run:
                filters = filters_map.get(trade.symbol)
                if filters is None:
                    logger.warning("Recovery (DRY_RUN): no filters for %s, cannot safely restore; skipping.", trade.symbol)
                    repo.update_trade(trade.id, status=STATUS_NEEDS_MANUAL_REVIEW)
                    report.mismatches += 1
                    blocked_symbols.add(trade.symbol)
                    continue
                position_manager.restore_position(trade, filters)
                report.restored += 1
                continue

            await _recover_live_trade(trade, settings, rest, repo, position_manager, risk_manager,
                                       notifier, filters_map, report, blocked_symbols)
        except Exception:
            logger.exception("Recovery failed unexpectedly for trade id=%s symbol=%s; flagging for manual review.",
                              trade.id, trade.symbol)
            repo.update_trade(trade.id, status=STATUS_NEEDS_MANUAL_REVIEW)
            report.mismatches += 1
            blocked_symbols.add(trade.symbol)
            await notifier.send(NotificationMessages.state_mismatch(trade.symbol, "unexpected exception during recovery"))

    await notifier.send(NotificationMessages.recovery_completed(report.restored, report.mismatches))
    logger.info("Recovery complete: restored=%d mismatches=%d", report.restored, report.mismatches)
    return report


async def _recover_live_trade(
    trade: Trade,
    settings: Settings,
    rest: BinanceRestClient,
    repo: TradeRepository,
    position_manager: PositionManager,
    risk_manager: RiskManager,
    notifier: TelegramNotifier,
    filters_map: Dict[str, SymbolFilters],
    report: RecoveryReport,
    blocked_symbols: Set[str],
) -> None:
    filters = filters_map.get(trade.symbol)

    if trade.status == "PENDING_ENTRY":
        if trade.buy_order_id:
            order = await rest.get_order(trade.symbol, trade.buy_order_id)
            if order.get("status") == "FILLED":
                logger.warning(
                    "Recovery: trade id=%s %s BUY actually FILLED on Binance but local setup never "
                    "completed (crashed mid-entry). Flagging for manual review rather than guessing "
                    "TP/ATR levels.", trade.id, trade.symbol,
                )
                repo.update_trade(trade.id, status=STATUS_NEEDS_MANUAL_REVIEW)
                report.mismatches += 1
                blocked_symbols.add(trade.symbol)
                await notifier.send(NotificationMessages.state_mismatch(
                    trade.symbol, "BUY filled on Binance but local entry setup incomplete"
                ))
                return
        repo.update_trade(trade.id, status=STATUS_REJECTED)
        logger.info("Recovery: trade id=%s %s was PENDING_ENTRY and never filled; marked REJECTED.",
                     trade.id, trade.symbol)
        return

    # OPEN or CLOSING
    if filters is None:
        logger.warning("Recovery: no exchange filters cached for %s; cannot safely verify. Flagging.", trade.symbol)
        repo.update_trade(trade.id, status=STATUS_NEEDS_MANUAL_REVIEW)
        report.mismatches += 1
        blocked_symbols.add(trade.symbol)
        return

    held_qty = await _held_quantity(rest, filters.base_asset)

    tp_order = None
    if trade.tp_order_id:
        tp_order = await rest.get_order(trade.symbol, trade.tp_order_id)

    if tp_order and tp_order.get("status") == "FILLED":
        executed_qty = float(tp_order.get("executedQty", trade.executed_qty))
        cumulative_quote = float(tp_order.get("cummulativeQuoteQty", 0.0))
        fill_price = (cumulative_quote / executed_qty) if executed_qty else trade.tp_price
        pnl = (fill_price - trade.avg_entry_price) * executed_qty
        repo.update_trade(
            trade.id, status=STATUS_CLOSED, exit_price=fill_price, exit_time=utcnow().isoformat(),
            exit_reason="TAKE_PROFIT_DURING_DOWNTIME", pnl_usdt=pnl,
        )
        for event in risk_manager.register_closed_trade(pnl):
            if event.kind == "DAILY_STOP":
                await notifier.send(NotificationMessages.daily_stop_triggered(event.message))
            elif event.kind == "PAUSE_TRIGGERED":
                await notifier.send(NotificationMessages.pause_triggered(event.message))
        logger.info("Recovery: %s was closed by TP fill while the bot was offline. PnL=%.4f", trade.symbol, pnl)
        await notifier.send(NotificationMessages.take_profit_executed(trade.symbol, fill_price, pnl))
        return

    if tp_order and tp_order.get("status") in ("CANCELED", "EXPIRED", "REJECTED"):
        logger.warning(
            "Recovery: TP order for %s is %s with no matching local close record. Flagging for manual "
            "review instead of guessing what happened while offline.", trade.symbol, tp_order.get("status"),
        )
        repo.update_trade(trade.id, status=STATUS_NEEDS_MANUAL_REVIEW)
        report.mismatches += 1
        blocked_symbols.add(trade.symbol)
        await notifier.send(NotificationMessages.state_mismatch(
            trade.symbol, f"TP order ended {tp_order.get('status')} with no recorded exit"
        ))
        return

    expected_qty = float(trade.executed_qty)
    if expected_qty <= 0 or held_qty < expected_qty * (1 - _CONSISTENCY_TOLERANCE):
        logger.warning(
            "Recovery: local DB expects to hold %.8f %s for %s but Binance shows %.8f. Mismatch flagged.",
            expected_qty, filters.base_asset, trade.symbol, held_qty,
        )
        repo.update_trade(trade.id, status=STATUS_NEEDS_MANUAL_REVIEW)
        report.mismatches += 1
        blocked_symbols.add(trade.symbol)
        await notifier.send(NotificationMessages.state_mismatch(
            trade.symbol, f"expected qty {expected_qty}, Binance holds {held_qty}"
        ))
        return

    position_manager.restore_position(trade, filters)
    report.restored += 1
    logger.info("Recovery: %s reconciled cleanly and restored (qty=%.8f, active_atr_stop=%s).",
                 trade.symbol, held_qty, trade.active_atr_stop)
