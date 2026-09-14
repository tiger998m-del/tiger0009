"""
Professional rotating logger with automatic secret redaction.

No API key, API secret, or Telegram bot token is ever written to a log file
or the terminal, even by accident: a logging.Filter scrubs any configured
secret value out of every record before it is emitted.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Iterable

_REDACTED = "***REDACTED***"


class SecretRedactionFilter(logging.Filter):
    """Removes known secret substrings (and bot-token-shaped URLs) from records."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]
        # Redact Telegram bot-token patterns embedded in URLs: /bot<token>/
        self._token_url_re = re.compile(r"(/bot)[A-Za-z0-9:_-]{20,}(/)")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for secret in self._secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, _REDACTED)
        redacted = self._token_url_re.sub(rf"\1{_REDACTED}\2", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(
    log_dir: str,
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
    secrets: Iterable[str] = (),
) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("binance_spot_bot")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    if root.handlers:
        # Already configured (e.g. re-entrant call in tests).
        return root

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    redaction_filter = SecretRedactionFilter(secrets)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(log_dir, "bot.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redaction_filter)

    error_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(log_dir, "errors.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    error_handler.addFilter(redaction_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.addFilter(redaction_filter)

    root.addHandler(file_handler)
    root.addHandler(error_handler)
    root.addHandler(console_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"binance_spot_bot.{name}")
