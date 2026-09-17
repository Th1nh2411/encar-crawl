"""
db.py — The MySQL connection, the run lock, and the `crawl_batches` bookkeeping
every crawl entry point shares.

A batch row is opened before the crawl starts and closed after it, so a run that
crashes is still visible as one that never finished. The advisory lock is what
keeps two overlapping cron runs from marking each other's listings missing.
"""

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def connect():
    import pymysql

    missing = [k for k in ("DB_HOST", "DB_NAME", "DB_USERNAME") if not os.getenv(k)]
    if missing:
        raise SystemExit(
            f"Missing DB env vars: {', '.join(missing)}. "
            "Set them in .env, or run with --no-db to crawl without a database."
        )
    return pymysql.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USERNAME"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME"),
        charset="utf8mb4",
        autocommit=False,
    )


def ident(name: str, what: str) -> str:
    """Quote a configured table/column name after validating it."""
    if not IDENT_RE.match(name):
        raise ValueError(f"Invalid {what} name in config: {name!r}")
    return f"`{name}`"


# ── Run lock ──────────────────────────────────────────────────────────────────
def lock_name(prefix: str) -> str:
    return f"{prefix}:{os.getenv('DB_NAME', 'encar')}"


def acquire_lock(conn, name: str, timeout: int = 0) -> bool:
    """MySQL advisory lock, so two overlapping cron runs cannot both mark SOLD.

    The lock is tied to this connection and released when it closes, including
    on a crash — no stale lock file to clean up by hand.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, %s)", (name, timeout))
        row = cur.fetchone()
    return bool(row and row[0] == 1)


def release_lock(conn, name: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:  # the connection is going away anyway
        pass


# ── crawl_batches ─────────────────────────────────────────────────────────────
def previous_ok_count(cur, model_key: str) -> Optional[int]:
    cur.execute(
        "SELECT crawled_count FROM crawl_batches "
        "WHERE model_key = %s AND status = 'OK' ORDER BY id DESC LIMIT 1",
        (model_key,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def known_details(cur, model_key: str) -> Dict[str, Dict[str, Any]]:
    """Detail fields already stored for this model, keyed by car_id.

    The vehicle number costs one detail request per car and never changes once
    known, so a car already carrying one in `vehicles` does not need asking
    about again. On a daily crawl that is the difference between a request for
    every car every day and a request for the handful newly listed — and Encar
    rate limits on request count, so it is also what keeps the crawl under the
    limit as the number of models grows.

    Only the fields that come from the detail payload and do not change:
    mileage and price move, and they come from the listing anyway.
    """
    cur.execute(
        "SELECT car_id, vehicle_no, model_info, manufacturer, model_group, "
        "model_name, grade_name, form_year, `year_month`, fuel_name, "
        "transmission, color_name FROM vehicles "
        "WHERE model_key = %s AND vehicle_no IS NOT NULL",
        (model_key,),
    )
    fields = ("vehicle_no", "model_info", "manufacturer", "model_group",
              "model_name", "grade_name", "form_year", "year_month",
              "fuel_name", "transmission", "color_name")
    return {str(row[0]): dict(zip(fields, row[1:])) for row in cur.fetchall()}


def open_batch(cur, model_key: str, started_at: datetime) -> int:
    cur.execute(
        "INSERT INTO crawl_batches (model_key, started_at, status) "
        "VALUES (%s, %s, 'RUNNING')",
        (model_key, started_at),
    )
    return int(cur.lastrowid)


def close_batch(cur, batch_id: int, metrics: Dict[str, Any], status: str,
                comparison_ran: bool, sold_count: int, note: str) -> None:
    cur.execute(
        "UPDATE crawl_batches SET finished_at = %s, total_expected_count = %s, "
        "total_expected_pages = %s, fetched_pages = %s, completion_rate = %s, "
        "crawled_count = %s, status = %s, comparison_ran = %s, "
        "sold_marked_count = %s, note = %s WHERE id = %s",
        (
            datetime.now(), metrics["total_expected_count"],
            metrics["total_expected_pages"], metrics["fetched_pages"],
            metrics["completion_rate"], len(metrics["rows"]), status,
            1 if comparison_ran else 0, sold_count, note[:500] or None, batch_id,
        ),
    )


def fail_batch(conn, cur, batch_id: int, note: str, log=print) -> None:
    """Close a batch as FAILED. Best effort: never mask the original error."""
    try:
        conn.rollback()
        cur.execute(
            "UPDATE crawl_batches SET finished_at = %s, status = 'FAILED', note = %s "
            "WHERE id = %s AND status = 'RUNNING'",
            (datetime.now(), note[:500], batch_id),
        )
        conn.commit()
    except Exception as exc:
        log(f"  ! could not close batch #{batch_id}: {type(exc).__name__}: {exc}")


def insert_crawled_vehicles(cur, batch_id: int, rows: List[Dict[str, Any]],
                            crawled_at: datetime) -> None:
    """The per-batch snapshot `sync_sold.py` compares `stock` against."""
    if not rows:
        return
    cur.executemany(
        "INSERT INTO crawled_vehicles "
        "(crawl_batch_id, car_id, vehicle_no, model_info, is_duplicate, crawled_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [
            (batch_id, r["car_id"], r["vehicle_no"], r["model_info"],
             r["is_duplicate"], crawled_at)
            for r in rows
        ],
    )
