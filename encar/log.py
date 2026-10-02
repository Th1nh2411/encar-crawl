"""
log.py -- The one logging call, plus the file it can tee into.

Progress goes to stderr so stdout stays free for a JSON document (--print).
Once setup_file_log() has run, every line is also appended to a rotating file:
cron discards the console, and the dashboard reads that file.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

_file_logger: Optional[logging.Logger] = None


def setup_file_log(path: str) -> str:
    """Tee log() into a rotating file. Returns the resolved path."""
    global _file_logger
    from logging.handlers import RotatingFileHandler

    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    logger = logging.getLogger(f"encar.{path}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            path,
            maxBytes=int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024))),
            backupCount=int(os.getenv("LOG_BACKUPS", "7")),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)

    _file_logger = logger
    return path


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    if _file_logger is not None:
        # The console uses blank lines and indentation; the file wants one
        # timestamped record per line.
        for line in msg.splitlines():
            if line.strip():
                _file_logger.info(line.rstrip())


def fmt_duration(seconds: float) -> str:
    """3.4 -> '3.40s'   |   135.2 -> '2m 15.20s'"""
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:.2f}s"
