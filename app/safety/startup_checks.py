"""
Startup Safety Check (constitution #7).

Because DRY_RUN defaults to false (LIVE TRADING MODE with real money), the
bot refuses to send its first real order until every critical check below
has passed. Nothing here ever prints or logs the API key/secret -- only
pass/fail booleans and human-readable, secret-free details.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import List

import websockets

from app.config.settings import Settings
from app.exchange.rest_client import BinanceRestClient
from app.utils.logging_setup import get_logger

logger = get_logger("safety.startup")


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str
    critical: bool


@dataclass
class StartupCheckReport:
    items: List[CheckItem]

    @property
    def passed(self) -> bool:
        return all(item.ok for item in self.items if item.critical)

    def summary_lines(self) -> List[str]:
        lines = []
        for item in self.items:
            mark = "OK" if item.ok else ("FAIL" if item.critical else "WARN")
            lines.append(f"[{mark}] {item.name}: {item.detail}")
        return lines


async def run_startup_checks(settings: Settings, rest: BinanceRestClient) -> StartupCheckReport:
    items: List[CheckItem] = []

    # 1. Basic connectivity
    try:
        await rest.ping()
        items.append(CheckItem("Binance REST connectivity", True, "ping OK", True))
    except Exception as exc:
        items.append(CheckItem("Binance REST connectivity", False, str(exc), True))

    # 2. Server time & clock drift
    try:
        t0 = time.time() * 1000
        server_time = await rest.server_time()
        t1 = time.time() * 1000
        local_mid = (t0 + t1) / 2
        drift_ms = server_time - local_mid
        rest.set_server_time_offset(int(server_time - t1))
        ok = abs(drift_ms) <= settings.time_drift_max_ms
        items.append(CheckItem(
            "Binance server time drift", ok,
            f"drift={drift_ms:.0f}ms (max allowed {settings.time_drift_max_ms}ms)", True,
        ))
    except Exception as exc:
        items.append(CheckItem("Binance server time drift", False, str(exc), True))

    # 3. Exchange info / symbol metadata
    try:
        info = await rest.exchange_info()
        n = len(info.get("symbols", []))
        items.append(CheckItem("Exchange info (symbols/filters)", n > 0, f"{n} symbols loaded", True))
    except Exception as exc:
        items.append(CheckItem("Exchange info (symbols/filters)", False, str(exc), True))

    # 4. Public market data access
    try:
        klines = await rest.klines("BTCUSDT", settings.kline_interval, 5)
        items.append(CheckItem("Market data access (klines)", len(klines) > 0,
                                f"{len(klines)} candles fetched", True))
    except Exception as exc:
        items.append(CheckItem("Market data access (klines)", False, str(exc), True))

    # 5. Account / credentials / permissions / balance
    should_check_account = (not settings.dry_run) or bool(settings.binance_api_key)
    if should_check_account:
        try:
            account = await rest.account_info()
            items.append(CheckItem("API key/secret authentication", True, "signed request accepted", True))

            can_trade = bool(account.get("canTrade", False))
            items.append(CheckItem("Spot trading permission (canTrade)", can_trade,
                                    f"canTrade={can_trade}", not settings.dry_run))

            account_type = account.get("accountType", "UNKNOWN")
            permissions = account.get("permissions", [])
            spot_ok = account_type == "SPOT" or "SPOT" in permissions
            items.append(CheckItem("SPOT account type/permission", spot_ok,
                                    f"accountType={account_type} permissions={permissions}",
                                    not settings.dry_run))

            balances = account.get("balances", [])
            items.append(CheckItem("Account balances readable", True, f"{len(balances)} assets", True))

            free_quote = 0.0
            for b in balances:
                if b.get("asset") == settings.quote_asset:
                    free_quote = float(b.get("free", 0.0))
                    break
            required = settings.capital_usdt
            enough = free_quote >= required
            items.append(CheckItem(
                f"Capital available ({settings.quote_asset})", enough,
                f"free={free_quote:.2f} required~={required:.2f}", not settings.dry_run,
            ))
        except Exception as exc:
            items.append(CheckItem("API key/secret authentication", False, str(exc), True))
    else:
        items.append(CheckItem("API credentials", True, "skipped: DRY_RUN with no API key configured", False))

    # 6. WebSocket connectivity
    try:
        url = f"{settings.binance_ws_base_url.rstrip('/')}/stream?streams=btcusdt@kline_1m"
        async with websockets.connect(url, open_timeout=10, close_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
        items.append(CheckItem("WebSocket connectivity", True, "connected and received data", True))
    except Exception as exc:
        items.append(CheckItem("WebSocket connectivity", False, str(exc), True))

    report = StartupCheckReport(items)
    for line in report.summary_lines():
        logger.info("Startup check -> %s", line)
    return report
