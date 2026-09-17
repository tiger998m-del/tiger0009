"""Sends a daily trading summary to Telegram. Meant to be run once a day via
cron (see README "Daily Telegram Report" section for setup).

Reads the same .env as the bot (BOT_CAPITAL_SAR, SAR_PER_USDT,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DB_PATH) and summarizes CLOSED trades
for the current UTC calendar day plus the bot's all-time record. Read-only
against the database -- never touches trading state -- so it is safe to run
alongside the live bot process.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def utc_date_str() -> str:
    """Mirrors app.utils.time_utils.utc_date_str without importing the app
    package, so this script has no dependency on the repo root being on
    sys.path (it is invoked directly as `python scripts/daily_report.py`)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

_TRADE_STATS_QUERY = """
    SELECT COUNT(*) AS trades,
           SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
           SUM(CASE WHEN pnl_usdt < 0 THEN 1 ELSE 0 END) AS losses,
           COALESCE(SUM(pnl_usdt), 0.0) AS pnl
    FROM trades
    WHERE status = 'CLOSED'{extra}
"""


def fetch_stats(db_path: str, today: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        today_row = conn.execute(
            _TRADE_STATS_QUERY.format(extra=" AND exit_time LIKE ?"), (f"{today}%",)
        ).fetchone()
        alltime_row = conn.execute(_TRADE_STATS_QUERY.format(extra="")).fetchone()
        open_rows = conn.execute(
            "SELECT symbol, capital_used_usdt FROM trades WHERE status = 'OPEN'"
        ).fetchall()
        risk_row = conn.execute(
            "SELECT daily_realized_pnl_usdt, daily_stopped, consecutive_losses, "
            "pause_until, effective_capital_usdt FROM risk_state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    return {
        "today": dict(today_row),
        "alltime": dict(alltime_row),
        "open_trades": [dict(r) for r in open_rows],
        "risk": dict(risk_row) if risk_row else {},
    }


def format_message(stats: dict, today: str) -> str:
    t, a, risk = stats["today"], stats["alltime"], stats["risk"]

    t_trades, t_wins, t_losses = t["trades"] or 0, t["wins"] or 0, t["losses"] or 0
    t_pnl = t["pnl"] or 0.0
    t_winrate = (t_wins / t_trades * 100) if t_trades else 0.0

    a_trades, a_wins = a["trades"] or 0, a["wins"] or 0
    a_pnl = a["pnl"] or 0.0
    a_winrate = (a_wins / a_trades * 100) if a_trades else 0.0

    lines = [
        f"DAILY REPORT -- {today} UTC",
        "",
        "Today:",
        f"  Closed trades: {t_trades} (W:{t_wins} / L:{t_losses}, {t_winrate:.0f}% win rate)",
        f"  Realized PnL: {t_pnl:+.2f} USDT",
    ]
    if risk:
        lines.append(f"  Daily Stop: {'ACTIVE' if risk.get('daily_stopped') else 'not triggered'}")
        if risk.get("pause_until"):
            lines.append(f"  Paused until: {risk['pause_until']}")
        lines.append(f"  Effective capital: {risk.get('effective_capital_usdt', 0.0):.2f} USDT")

    lines += ["", f"Open positions now: {len(stats['open_trades'])}"]
    for pos in stats["open_trades"]:
        lines.append(f"  - {pos['symbol']} ({pos['capital_used_usdt']:.2f} USDT)")

    lines += [
        "",
        "All-time:",
        f"  Closed trades: {a_trades} (W:{a_wins}, {a_winrate:.0f}% win rate)",
        f"  Cumulative PnL: {a_pnl:+.2f} USDT",
    ]
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main() -> None:
    db_path = os.getenv("DB_PATH", "data/bot.db")
    if not os.path.isabs(db_path):
        db_path = str(REPO_ROOT / db_path)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env")

    today = utc_date_str()
    stats = fetch_stats(db_path, today)
    message = format_message(stats, today)
    send_telegram(token, chat_id, message)
    print(message)


if __name__ == "__main__":
    main()
