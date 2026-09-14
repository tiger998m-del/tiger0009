#!/usr/bin/env python3
"""
Entry point for the Binance Spot Trading Bot.

Usage:
    python main.py

Configuration is read exclusively from a ".env" file in the current working
directory (see .env.example). No secrets or trading parameters are ever
hard-coded in source.
"""

from __future__ import annotations

import asyncio
import sys

from app.config.settings import ConfigError
from app.core.bot import create_and_run_bot


def main() -> int:
    try:
        asyncio.run(create_and_run_bot())
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1
    except KeyboardInterrupt:
        print("Interrupted by user.")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
