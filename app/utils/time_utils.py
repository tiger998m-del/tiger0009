"""Small UTC time helpers shared across the codebase.

All bot-internal timestamps are UTC. The "trading day" for the Daily Stop
rule is defined as the UTC calendar day (00:00 - 23:59:59 UTC). This is a
documented engineering decision (see README) since Binance itself has no
concept of a "trading day".
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utc_date_str(dt: datetime = None) -> str:
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def datetime_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
