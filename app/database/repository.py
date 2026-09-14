"""
SQLite persistence layer. Trades are never held in RAM only -- every state
transition (opened, ATR stop updated, closing, closed) is written through to
disk immediately so a crash/restart can recover exactly where the bot left
off (see app/core/recovery.py).

sqlite3 is used directly (WAL mode) rather than an ORM: the schema is small,
fixed, and benefits from being fully transparent/auditable. All access goes
through a single `threading.Lock` because sqlite3 connections are not
safe for concurrent use from multiple coroutines/threads at once, even
though this bot is predominantly single-threaded asyncio.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import List, Optional

from app.database.models import RiskState, Trade
from app.utils.time_utils import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    buy_order_id INTEGER,
    sell_order_id INTEGER,
    tp_order_id INTEGER,
    entry_time TEXT,
    avg_entry_price REAL NOT NULL DEFAULT 0,
    executed_qty REAL NOT NULL DEFAULT 0,
    capital_used_usdt REAL NOT NULL DEFAULT 0,
    tp_price REAL NOT NULL DEFAULT 0,
    initial_atr_stop REAL NOT NULL DEFAULT 0,
    active_atr_stop REAL NOT NULL DEFAULT 0,
    best_stop REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING_ENTRY',
    exit_reason TEXT,
    exit_price REAL,
    exit_time TEXT,
    pnl_usdt REAL,
    fees_usdt REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol_status ON trades(symbol, status);

CREATE TABLE IF NOT EXISTS risk_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    daily_date TEXT NOT NULL,
    daily_realized_pnl_usdt REAL NOT NULL DEFAULT 0,
    daily_stopped INTEGER NOT NULL DEFAULT 0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    pause_until TEXT,
    updated_at TEXT NOT NULL
);
"""

_OPEN_LIKE_STATUSES = ("PENDING_ENTRY", "OPEN", "CLOSING")


class TradeRepository:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- Trades
    def create_trade(self, trade: Trade) -> int:
        now = utcnow().isoformat()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO trades
                   (symbol, buy_order_id, status, capital_used_usdt, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (trade.symbol, trade.buy_order_id, trade.status, trade.capital_used_usdt, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_trade(self, trade_id: int, **fields) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated_at"] = utcnow().isoformat()
        columns = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [trade_id]
        with self._lock:
            self._conn.execute(f"UPDATE trades SET {columns} WHERE id = ?", values)
            self._conn.commit()

    def get_trade(self, trade_id: int) -> Optional[Trade]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return self._row_to_trade(row) if row else None

    def get_open_trades(self) -> List[Trade]:
        placeholders = ",".join("?" for _ in _OPEN_LIKE_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM trades WHERE status IN ({placeholders}) ORDER BY id ASC",
                _OPEN_LIKE_STATUSES,
            ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_open_trade_for_symbol(self, symbol: str) -> Optional[Trade]:
        placeholders = ",".join("?" for _ in _OPEN_LIKE_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM trades WHERE symbol = ? AND status IN ({placeholders}) "
                "ORDER BY id DESC LIMIT 1",
                (symbol, *_OPEN_LIKE_STATUSES),
            ).fetchone()
        return self._row_to_trade(row) if row else None

    def get_recent_closed_trades(self, limit: int = 20) -> List[Trade]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_all_trades(self) -> List[Trade]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
        return [self._row_to_trade(r) for r in rows]

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> Trade:
        return Trade(**{k: row[k] for k in row.keys()})

    # ---------------------------------------------------------- Risk state
    def get_risk_state(self) -> Optional[RiskState]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM risk_state WHERE id = 1").fetchone()
        if not row:
            return None
        return RiskState(
            daily_date=row["daily_date"],
            daily_realized_pnl_usdt=row["daily_realized_pnl_usdt"],
            daily_stopped=bool(row["daily_stopped"]),
            consecutive_losses=row["consecutive_losses"],
            pause_until=row["pause_until"],
            updated_at=row["updated_at"],
        )

    def save_risk_state(self, state: RiskState) -> None:
        now = utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO risk_state
                       (id, daily_date, daily_realized_pnl_usdt, daily_stopped, consecutive_losses, pause_until, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       daily_date=excluded.daily_date,
                       daily_realized_pnl_usdt=excluded.daily_realized_pnl_usdt,
                       daily_stopped=excluded.daily_stopped,
                       consecutive_losses=excluded.consecutive_losses,
                       pause_until=excluded.pause_until,
                       updated_at=excluded.updated_at
                """,
                (
                    state.daily_date,
                    state.daily_realized_pnl_usdt,
                    int(state.daily_stopped),
                    state.consecutive_losses,
                    state.pause_until,
                    now,
                ),
            )
            self._conn.commit()
