"""
encar_stock_sync.py — Crawl Encar listings for specific models and mark
vehicles that disappeared from Encar as SOLD in our `stock` table.

Approach
--------
The Encar search API is called directly (no browser, no detail pages):

    GET https://api.encar.com/search/car/list/premium
        ?count=true
        &q=(And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.<M>._.(C.ModelGroup.<G>._.Model.<M2>.))))
        &sr=|ModifiedDate|<offset>|<limit>

`Hidden.N` = listing is currently advertised. A car in `stock` whose Encar
listing id no longer appears in the crawl is considered SOLD.

Matching key = Encar listing id (`car_id`), NOT the vehicle number.
Verified against the live API: neither /search/car/list/premium nor
/search/car/list/general returns `vehicleNo` — that field only exists on the
per-vehicle detail endpoint. Matching on car_id keeps the crawl at ~1 request
per 200 listings instead of 1 request per vehicle.

Safeguards (a partial crawl must never mark live cars as sold)
--------------------------------------------------------------
- completion_rate = fetched_pages / total_expected_pages
    >= 0.99  -> run the comparison
    >= 0.95  -> batch PARTIAL, comparison skipped
    <  0.95  -> batch FAILED, comparison skipped
- zero-row guard: a batch with 0 rows never runs the comparison
- drop guard: crawled_count more than 50% below the previous OK batch for the
  same model skips the comparison (catches silent blocking / markup changes)

A skipped batch changes no `stock` rows; the next healthy run does the work.

Usage
-----
    pip install requests pymysql python-dotenv

    # Crawl only, no DB — writes a JSON snapshot (use this to smoke-test)
    python encar_stock_sync.py --no-db --json out/snapshot.json

    # Full run against the DB, but report the SOLD candidates without writing
    python encar_stock_sync.py --dry-run

    # Full run
    python encar_stock_sync.py

    # Restrict to some models from models.json
    python encar_stock_sync.py --model bmw_3series_g20
"""

import argparse
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, os.pardir, ".env"))
load_dotenv(os.path.join(HERE, ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_URL = "https://api.encar.com/search/car/list/premium"
DETAIL_URL = "https://api.encar.com/v1/readside/vehicle/{car_id}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.encar.com/fc/fc_carsearchlist.do?carType=for",
    "Origin": "https://www.encar.com",
}

PAGE_SIZE = int(os.getenv("SYNC_PAGE_SIZE", "200"))
LIST_WORKERS = int(os.getenv("SYNC_LIST_WORKERS", "4"))
# Kept deliberately modest: the detail pass is ~1200 requests for one model and
# 8 workers put it around 40 req/s, which is what tripped the CloudFront rate
# limit above. A daily run has no reason to be that fast.
DETAIL_WORKERS = int(os.getenv("SYNC_DETAIL_WORKERS", "4"))
MAX_RETRY = int(os.getenv("SYNC_MAX_RETRY", "4"))
BASE_BACKOFF = float(os.getenv("SYNC_BASE_BACKOFF", "3.0"))

# Encar sits behind CloudFront and answers a throttled client with 407 (also
# seen as 403/429), Content-Length 0 and `X-Cache: Error from cloudfront`. That
# is a rate limit, not a proxy or session problem: plain curl from the same host
# gets the identical 407. Retrying a few seconds later just burns the attempts,
# so these statuses back off on their own, much slower, scale.
THROTTLE_STATUSES = (403, 407, 429)
THROTTLE_BACKOFF = float(os.getenv("SYNC_THROTTLE_BACKOFF", "45.0"))
REQUEST_TIMEOUT = float(os.getenv("SYNC_REQUEST_TIMEOUT", "25"))

# Safeguard thresholds — config, not hardcoded policy (see module docstring).
THRESHOLD_OK = float(os.getenv("SYNC_THRESHOLD_OK", "0.99"))
THRESHOLD_PARTIAL = float(os.getenv("SYNC_THRESHOLD_PARTIAL", "0.95"))
MAX_DROP_RATIO = float(os.getenv("SYNC_MAX_DROP_RATIO", "0.5"))

# `stock` table shape — this repo does not own that schema, so it is configurable.
STOCK_TABLE = os.getenv("STOCK_TABLE", "stock")
STOCK_CAR_ID_COL = os.getenv("STOCK_CAR_ID_COL", "car_id")
STOCK_STATUS_COL = os.getenv("STOCK_STATUS_COL", "status")
STOCK_SOLD_AT_COL = os.getenv("STOCK_SOLD_AT_COL", "sold_confirmed_at")
STOCK_SOLD_VALUE = os.getenv("STOCK_SOLD_VALUE", "SOLD")

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_file_logger: Optional[logging.Logger] = None


def setup_file_log(path: str) -> str:
    """Tee log() into a rotating file. Cron discards stdout; this is the record.

    Returns the resolved path so the caller can print it.
    """
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


def db_lock_name(prefix: str) -> str:
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


def ident(name: str, what: str) -> str:
    """Quote a configured table/column name after validating it."""
    if not IDENT_RE.match(name):
        raise ValueError(f"Invalid {what} name in config: {name!r}")
    return f"`{name}`"


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:.2f}s"


# ── Encar API ─────────────────────────────────────────────────────────────────
_local = threading.local()


def backoff_for(attempt: int, throttled: bool) -> float:
    """Seconds to wait before the next attempt."""
    if throttled:
        return THROTTLE_BACKOFF * (2 ** attempt) + random.uniform(0, 5)
    return BASE_BACKOFF * (attempt + 1) + random.uniform(0, 1.5)


def reset_session() -> None:
    """Drop this thread's session so the next call re-does the cookie warm-up."""
    session = getattr(_local, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
        _local.session = None


def get_session() -> requests.Session:
    """One Session per thread — keep-alive + cookies is what avoids HTTP 407."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        if os.getenv("SYNC_USE_SYSTEM_PROXY", "0") != "1":
            # macOS system proxy settings leak in through trust_env and answer
            # every Encar call with HTTP 407. Opt in explicitly if a proxy is wanted.
            session.trust_env = False
        try:
            session.get("https://www.encar.com/", timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            pass
        _local.session = session
    return session


def build_query(spec: Dict[str, str]) -> str:
    """Search expression for one model. requests handles the URL-encoding."""
    return (
        "(And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.{manufacturer}._."
        "(C.ModelGroup.{model_group}._.Model.{model}.))))"
    ).format(**spec)


def fetch_page(query: str, offset: int, limit: int, want_count: bool) -> Dict[str, Any]:
    """One listing page, with retry/backoff. Raises RuntimeError when exhausted."""
    params = {
        "count": "true" if want_count else "false",
        "q": query,
        "sr": f"|ModifiedDate|{offset}|{limit}",
    }
    last_err: Optional[str] = None
    throttled = False

    for attempt in range(MAX_RETRY):
        try:
            resp = get_session().get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            throttled = False
        else:
            if resp.status_code == 200:
                return resp.json() if resp.content else {}
            last_err = f"HTTP {resp.status_code}"
            throttled = resp.status_code in THROTTLE_STATUSES
            if throttled:
                reset_session()

        if attempt < MAX_RETRY - 1:
            # Short backoff for ordinary hiccups — a few slow pages must not
            # stall the batch — but a rate limit needs real time to clear.
            wait = backoff_for(attempt, throttled)
            log(f"    ... offset={offset} {last_err}, retry in {wait:.1f}s")
            time.sleep(wait)

    raise RuntimeError(last_err or "unknown error")


def fetch_detail(car_id: str) -> Optional[Dict[str, Any]]:
    """Per-vehicle detail. Returns None when every retry failed."""
    last_err: Optional[str] = None
    throttled = False
    for attempt in range(MAX_RETRY):
        try:
            resp = get_session().get(DETAIL_URL.format(car_id=car_id), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            throttled = False
        else:
            if resp.status_code == 200:
                return resp.json() if resp.content else None
            if resp.status_code == 404:  # listing pulled between the list and detail call
                return None
            last_err = f"HTTP {resp.status_code}"
            throttled = resp.status_code in THROTTLE_STATUSES
            if throttled:
                reset_session()

        if attempt < MAX_RETRY - 1:
            time.sleep(backoff_for(attempt, throttled))

    log(f"    ! detail {car_id} failed: {last_err}")
    return None


def enrich_vehicle_numbers(rows: List[Dict[str, Any]]) -> int:
    """Fill `vehicle_no` (and sharpen `model_info`) from the detail endpoint.

    The listing API does not expose the vehicle number, so this is one request
    per listing. Failures leave `vehicle_no` NULL — they must not affect the
    sold-detection gate, which matches on `car_id`.
    """
    def work(row: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        return row, fetch_detail(row["car_id"])

    filled = 0
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for row, detail in pool.map(work, rows):
            if not detail:
                continue
            vehicle_no = detail.get("vehicleNo")
            if vehicle_no:
                row["vehicle_no"] = str(vehicle_no)
                filled += 1
            category = detail.get("category") or {}
            parts = [
                category.get("manufacturerName"),
                category.get("modelName"),
                category.get("gradeName"),
                category.get("gradeDetailName"),
            ]
            detailed = " ".join(str(p) for p in parts if p).strip()
            if detailed:
                row["model_info"] = detailed
    return filled


def to_row(item: Dict[str, Any]) -> Dict[str, Any]:
    model_info = " ".join(
        str(p) for p in (item.get("Manufacturer"), item.get("Model"), item.get("Badge")) if p
    ).strip()
    return {
        "car_id": str(item.get("Id")),
        "vehicle_no": None,  # not exposed by the listing API
        "model_info": model_info or None,
        "is_duplicate": 1 if item.get("ServiceCopyCar") == "DUPLICATION" else 0,
    }


def crawl_model(spec: Dict[str, str], with_details: bool = False) -> Dict[str, Any]:
    """Crawl every listing page for one model. Returns rows + crawl metrics."""
    query = build_query(spec)
    started = time.perf_counter()

    first = fetch_page(query, 0, PAGE_SIZE, want_count=True)
    total_count = int(first.get("Count") or 0)
    total_pages = max(1, math.ceil(total_count / PAGE_SIZE)) if total_count else 0

    rows: Dict[str, Dict[str, Any]] = {}
    for item in first.get("SearchResults") or []:
        row = to_row(item)
        rows[row["car_id"]] = row

    fetched_pages = 1 if total_pages else 0
    failed_offsets: List[int] = []

    if total_pages > 1:
        offsets = [p * PAGE_SIZE for p in range(1, total_pages)]

        def work(offset: int) -> Tuple[int, Optional[List[Dict[str, Any]]]]:
            try:
                data = fetch_page(query, offset, PAGE_SIZE, want_count=False)
            except RuntimeError as exc:
                log(f"    ! page offset={offset} failed: {exc}")
                return offset, None
            return offset, data.get("SearchResults") or []

        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            for offset, items in pool.map(work, offsets):
                if items is None:
                    failed_offsets.append(offset)
                    continue
                fetched_pages += 1
                for item in items:
                    row = to_row(item)
                    rows[row["car_id"]] = row

    completion_rate = (fetched_pages / total_pages) if total_pages else 0.0
    all_rows = list(rows.values())

    vehicle_no_filled = 0
    if with_details and all_rows:
        log(f"    fetching {len(all_rows)} detail page(s) for vehicle numbers ...")
        vehicle_no_filled = enrich_vehicle_numbers(all_rows)
        log(f"    vehicle numbers: {vehicle_no_filled}/{len(all_rows)}")

    return {
        "rows": all_rows,
        "vehicle_no_filled": vehicle_no_filled,
        "total_expected_count": total_count,
        "total_expected_pages": total_pages,
        "fetched_pages": fetched_pages,
        "failed_offsets": failed_offsets,
        "completion_rate": round(completion_rate, 4),
        "elapsed": time.perf_counter() - started,
    }


# ── Safeguard gate ────────────────────────────────────────────────────────────
def evaluate(crawl: Dict[str, Any], previous_count: Optional[int]) -> Tuple[str, bool, str]:
    """-> (batch status, may the comparison run, note)"""
    rate = crawl["completion_rate"]
    count = len(crawl["rows"])

    if crawl["total_expected_pages"] == 0:
        return "FAILED", False, "API reported 0 listings for this model"
    if rate < THRESHOLD_PARTIAL:
        return "FAILED", False, (
            f"completion_rate {rate:.2%} < {THRESHOLD_PARTIAL:.0%} "
            f"({crawl['fetched_pages']}/{crawl['total_expected_pages']} pages)"
        )
    if rate < THRESHOLD_OK:
        return "PARTIAL", False, (
            f"completion_rate {rate:.2%} below the {THRESHOLD_OK:.0%} gate "
            f"({crawl['fetched_pages']}/{crawl['total_expected_pages']} pages)"
        )
    if count == 0:
        return "PARTIAL", False, "zero-row guard: crawl returned no rows"
    if previous_count and count < previous_count * (1 - MAX_DROP_RATIO):
        return "PARTIAL", False, (
            f"drop guard: {count} rows vs {previous_count} in the previous OK batch "
            f"(> {MAX_DROP_RATIO:.0%} drop)"
        )
    return "OK", True, ""


# ── DB ────────────────────────────────────────────────────────────────────────
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


def previous_ok_count(cur, model_key: str) -> Optional[int]:
    cur.execute(
        "SELECT crawled_count FROM crawl_batches "
        "WHERE model_key = %s AND status = 'OK' ORDER BY id DESC LIMIT 1",
        (model_key,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def open_batch(cur, model_key: str, started_at: datetime) -> int:
    cur.execute(
        "INSERT INTO crawl_batches (model_key, started_at, status) VALUES (%s, %s, 'RUNNING')",
        (model_key, started_at),
    )
    return int(cur.lastrowid)


def insert_rows(cur, batch_id: int, rows: List[Dict[str, Any]], crawled_at: datetime) -> None:
    if not rows:
        return
    cur.executemany(
        "INSERT INTO crawled_vehicles "
        "(crawl_batch_id, car_id, vehicle_no, model_info, is_duplicate, crawled_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [
            (batch_id, r["car_id"], r["vehicle_no"], r["model_info"], r["is_duplicate"], crawled_at)
            for r in rows
        ],
    )


def close_batch(cur, batch_id: int, crawl: Dict[str, Any], status: str,
                comparison_ran: bool, sold_count: int, note: str) -> None:
    cur.execute(
        "UPDATE crawl_batches SET finished_at = %s, total_expected_count = %s, "
        "total_expected_pages = %s, fetched_pages = %s, completion_rate = %s, "
        "crawled_count = %s, status = %s, comparison_ran = %s, sold_marked_count = %s, "
        "note = %s WHERE id = %s",
        (
            datetime.now(), crawl["total_expected_count"], crawl["total_expected_pages"],
            crawl["fetched_pages"], crawl["completion_rate"], len(crawl["rows"]),
            status, 1 if comparison_ran else 0, sold_count, note[:500] or None, batch_id,
        ),
    )


def compare_and_mark(cur, batch_id: int, spec: Dict[str, str], dry_run: bool) -> int:
    """Cars in `stock` for this model that are absent from the batch -> SOLD.

    `stock_filter` scopes the update to the model this batch actually crawled;
    without it every other model in `stock` would look 'missing' and be sold off.
    It comes from models.json (operator-controlled config) and is inlined as SQL.
    """
    stock_filter = (spec.get("stock_filter") or "").strip()
    if not stock_filter:
        raise ValueError(f"models.json entry {spec['key']!r} has no stock_filter")

    table = ident(STOCK_TABLE, "stock table")
    car_col = ident(STOCK_CAR_ID_COL, "stock car id column")
    status_col = ident(STOCK_STATUS_COL, "stock status column")
    sold_at_col = ident(STOCK_SOLD_AT_COL, "stock sold-at column")

    where = (
        f"WHERE {stock_filter} "
        f"AND s.{status_col} <> %s "
        f"AND s.{car_col} IS NOT NULL AND s.{car_col} <> '' "
        f"AND cv.id IS NULL"
    )
    join = (
        f"FROM {table} s "
        f"LEFT JOIN crawled_vehicles cv "
        f"  ON cv.car_id = s.{car_col} AND cv.crawl_batch_id = %s "
    )

    if dry_run:
        cur.execute(f"SELECT COUNT(*) {join} {where}", (batch_id, STOCK_SOLD_VALUE))
        return int(cur.fetchone()[0])

    cur.execute(
        f"UPDATE {table} s "
        f"LEFT JOIN crawled_vehicles cv "
        f"  ON cv.car_id = s.{car_col} AND cv.crawl_batch_id = %s "
        f"SET s.{status_col} = %s, s.{sold_at_col} = %s "
        f"{where}",
        (batch_id, STOCK_SOLD_VALUE, datetime.now(), STOCK_SOLD_VALUE),
    )
    return int(cur.rowcount)


# ── Run ───────────────────────────────────────────────────────────────────────
def load_specs(path: str, only: List[str]) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8") as f:
        specs = json.load(f)
    if only:
        wanted = set(only)
        specs = [s for s in specs if s["key"] in wanted]
        unknown = wanted - {s["key"] for s in specs}
        if unknown:
            raise SystemExit(f"Unknown model key(s): {', '.join(sorted(unknown))}")
    if not specs:
        raise SystemExit("No models selected")
    return specs


def run_model(conn, spec: Dict[str, str], dry_run: bool, with_details: bool = False,
              compare: bool = True) -> Dict[str, Any]:
    log(f"\n=== {spec['label']} ({spec['key']}) ===")
    crawled_at = datetime.now()

    cur = conn.cursor() if conn else None
    batch_id: Optional[int] = None
    if cur:
        batch_id = open_batch(cur, spec["key"], crawled_at)
        conn.commit()
        log(f"  batch #{batch_id}")

    crawl = crawl_model(spec, with_details=with_details)
    log(f"  {len(crawl['rows'])} listings | "
        f"{crawl['fetched_pages']}/{crawl['total_expected_pages']} pages | "
        f"completion {crawl['completion_rate']:.2%} | {fmt_duration(crawl['elapsed'])}")

    summary: Dict[str, Any] = {
        "model_key": spec["key"],
        "batch_id": batch_id,
        "crawled_count": len(crawl["rows"]),
        "vehicle_no_filled": crawl["vehicle_no_filled"],
        "completion_rate": crawl["completion_rate"],
        "fetched_pages": crawl["fetched_pages"],
        "total_expected_pages": crawl["total_expected_pages"],
        "failed_offsets": crawl["failed_offsets"],
        "rows": crawl["rows"],
    }

    if not cur:
        status, may_compare, note = evaluate(crawl, None)
        log(f"  status {status}" + (f" — {note}" if note else ""))
        summary.update(status=status, comparison_ran=False, sold_marked=0, note=note)
        return summary

    prev = previous_ok_count(cur, spec["key"])
    status, may_compare, note = evaluate(crawl, prev)
    if not compare:
        may_compare = False
        note = note or "comparison disabled (--no-compare)"

    insert_rows(cur, batch_id, crawl["rows"], crawled_at)

    sold_count = 0
    if may_compare:
        sold_count = compare_and_mark(cur, batch_id, spec, dry_run)
        verb = "would be marked" if dry_run else "marked"
        log(f"  status OK — {sold_count} vehicle(s) {verb} SOLD")
    else:
        log(f"  status {status} — comparison skipped: {note}")

    close_batch(cur, batch_id, crawl, status, may_compare and not dry_run, sold_count, note)
    if dry_run:
        conn.rollback()
        log("  dry-run: rolled back")
    else:
        conn.commit()

    summary.update(status=status, comparison_ran=may_compare, sold_marked=sold_count, note=note)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Encar listings per model and mark missing stock vehicles as SOLD."
    )
    parser.add_argument("--models-file", default=os.path.join(HERE, "models.json"))
    parser.add_argument("--model", action="append", default=[],
                        help="Model key to run (repeatable). Default: every model in models.json")
    parser.add_argument("--no-db", action="store_true",
                        help="Crawl only — no DB writes, no comparison")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything but roll back; reports the SOLD count")
    parser.add_argument("--no-compare", dest="compare", action="store_false",
                        help="Crawl and store only — skip the stock comparison / SOLD marking")
    parser.add_argument("--details", action="store_true",
                        help="Also fetch each listing's detail page to fill in the vehicle number "
                             "(1 extra request per listing)")
    parser.add_argument("--json", dest="json_out", help="Write the crawl snapshot to this file")
    args = parser.parse_args()

    specs = load_specs(args.models_file, args.model)
    conn = None if args.no_db else connect()

    started = time.perf_counter()
    summaries: List[Dict[str, Any]] = []
    failed = False
    try:
        for spec in specs:
            try:
                summaries.append(run_model(conn, spec, args.dry_run, args.details, args.compare))
            except Exception as exc:  # one bad model must not kill the batch
                failed = True
                log(f"  !! {spec['key']} failed: {type(exc).__name__}: {exc}")
                if conn:
                    conn.rollback()
    finally:
        if conn:
            conn.close()

    elapsed = time.perf_counter() - started
    log("\n" + "=" * 78)
    log(f"{'MODEL':<24}{'STATUS':<10}{'CRAWLED':<10}{'VEH.NO':<10}{'RATE':<10}{'SOLD':<8}NOTE")
    log("-" * 78)
    for s in summaries:
        log(f"{s['model_key']:<24}{s['status']:<10}{s['crawled_count']:<10}"
            f"{s.get('vehicle_no_filled', 0):<10}{s['completion_rate']:<10.2%}"
            f"{s.get('sold_marked', 0):<8}{s.get('note', '')}")
    log("=" * 78)
    log(f"Total: {fmt_duration(elapsed)}")

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(
                {"crawledAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "models": summaries},
                f, ensure_ascii=False, indent=2,
            )
        log(f"Wrote snapshot: {args.json_out}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
