"""
run_log.py — The one logging call the pipeline scripts share, plus the
duration formatting that goes with it.

log() prints to stdout and, once setup_file_log() has been called, also
appends to a rotating file. Cron discards stdout, so that file is the record
of what a run actually did.
"""

import logging
import os
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
    print(msg, flush=True)
    if _file_logger is not None:
        # The console layout uses blank lines and indentation; the file wants
        # one timestamped record per line.
        for line in msg.splitlines():
            if line.strip():
                _file_logger.info(line.rstrip())


def fmt_duration(seconds: float) -> str:
    """3.4 -> '3.40s'   |   135.2 -> '2m 15.20s'"""
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:.2f}s"
