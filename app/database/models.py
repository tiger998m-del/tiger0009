"""Data models mirroring the SQLite schema. Plain dataclasses -- no ORM,
to keep the persistence layer transparent and easy to audit."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Trade lifecycle states.
STATUS_PENDING_ENTRY = "PENDING_ENTRY"
STATUS_OPEN = "OPEN"
STATUS_CLOSING = "CLOSING"
STATUS_CLOSED = "CLOSED"
STATUS_NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
STATUS_REJECTED = "REJECTED"


@dataclass
class Trade:
    id: Optional[int] = None
    symbol: str = ""
    buy_order_id: Optional[int] = None
    sell_order_id: Optional[int] = None
    tp_order_id: Optional[int] = None
    entry_time: Optional[str] = None
    avg_entry_price: float = 0.0
    executed_qty: float = 0.0
    capital_used_usdt: float = 0.0
    tp_price: float = 0.0
    initial_atr_stop: float = 0.0
    active_atr_stop: float = 0.0
    best_stop: float = 0.0
    status: str = STATUS_PENDING_ENTRY
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl_usdt: Optional[float] = None
    fees_usdt: float = 0.0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class RiskState:
    daily_date: str = ""
    daily_realized_pnl_usdt: float = 0.0
    daily_stopped: bool = False
    consecutive_losses: int = 0
    pause_until: Optional[str] = None
    effective_capital_usdt: float = 0.0
    updated_at: Optional[str] = None
