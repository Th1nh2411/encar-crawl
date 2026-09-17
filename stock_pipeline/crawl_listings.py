"""
crawl_listings.py — The recurring crawl of the Encar listings for the models in
models.json, into the `vehicles` master table.

This is the half of the pipeline that owns `vehicles` and touches nothing else:
sync_sold.py owns the comparison against someone else's `stock` table. Here the
goal is a full, current list of the vehicles Encar advertises for a model, with
the car number and the model information.

    listing API -> listing ids + model/price/mileage  (1 request per 200 cars)
    detail API  -> vehicle number + grade/color       (1 request per car)

The vehicle number only exists on the detail endpoint, so the detail pass is
mandatory for the car-number requirement; use --no-details to skip it when you
only need the listing-level fields.

Rows are upserted on `car_id`: re-running refreshes the mutable fields and
bumps `last_seen_at`, keeping `first_seen_at` from the original insert.

Sold-out status
---------------
Each run opens a `crawl_batches` row and stamps its id on every listing it saw
(`vehicles.last_batch_id`). Rows of that model left carrying an older batch id
were absent from Encar this run, so they get `missing_streak + 1`; only once the
streak reaches SOLD_MISSING_STREAK (default 2) does the row become
`status = 'SOLD'` + `sold_detected_at`. One missed crawl is not a sale — a
dealer can pause an ad, and a short page still answers HTTP 200. A listing that
comes back resets its streak and returns to ACTIVE.

Nothing is marked unless the run passes guards.evaluate_batch() — the same gate
sync_sold.py uses. A blocked or half-finished crawl stores its rows and skips
the marking; the next healthy run does it. `--no-sold` disables it.

Running daily
-------------
    python stock_pipeline/migrate.py
    python stock_pipeline/crawl_listings.py --log-file logs/encar.log

One run at a time is enforced with a MySQL advisory lock, so an overlapping
cron start exits with code 2 instead of racing the run in flight. Exit codes:
0 ok, 1 at least one model failed, 2 another run holds the lock.

Read car-level status from the `vehicles_by_plate` view, not from
`vehicles.status`: the same car is often several listings, and it is only
really sold once every one of them is gone.

Usage
-----
    python stock_pipeline/crawl_listings.py --model bmw_3series_g20
    python stock_pipeline/crawl_listings.py                 # every model
    python stock_pipeline/crawl_listings.py --model bmw_x3_g01 --no-db --out-dir
"""

import argparse
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import results
from config import (
    DETAIL_WORKERS,
    LIST_WORKERS,
    MODEL_COOLDOWN,
    MODELS_FILE,
    PAGE_SIZE,
    ROOT_DIR,
    SOLD_MISSING_STREAK,
)
from db import (
    acquire_lock,
    close_batch,
    connect,
    fail_batch,
    lock_name,
    open_batch,
    previous_ok_count,
    release_lock,
)
from encar_api import (
    detail_model_info,
    fetch_listing_page,
    fetch_vehicle_detail,
    listing_model_info,
    model_query,
)
from guards import evaluate_batch
from models import load_specs
from run_log import fmt_duration, log, setup_file_log

TOOL = "crawl_listings"
DEFAULT_OUT_DIR = os.path.join(ROOT_DIR, "output", TOOL)
LOCK_PREFIX = "crawl_listings"

UPSERT_SQL = """
INSERT INTO vehicles
    (car_id, vehicle_no, model_key, model_info, manufacturer, model_group, model_name,
     grade_name, form_year, `year_month`, mileage, fuel_name, transmission, color_name,
     price, is_duplicate, status, sold_detected_at, last_batch_id, first_seen_at, last_seen_at)
VALUES (%(car_id)s, %(vehicle_no)s, %(model_key)s, %(model_info)s, %(manufacturer)s,
        %(model_group)s, %(model_name)s, %(grade_name)s, %(form_year)s, %(year_month)s,
        %(mileage)s, %(fuel_name)s, %(transmission)s, %(color_name)s, %(price)s,
        %(is_duplicate)s, 'ACTIVE', NULL, %(batch_id)s, %(seen_at)s, %(seen_at)s)
ON DUPLICATE KEY UPDATE
    vehicle_no   = COALESCE(VALUES(vehicle_no), vehicle_no),
    model_key    = VALUES(model_key),
    model_info   = COALESCE(VALUES(model_info), model_info),
    manufacturer = VALUES(manufacturer),
    model_group  = VALUES(model_group),
    model_name   = VALUES(model_name),
    grade_name   = COALESCE(VALUES(grade_name), grade_name),
    form_year    = VALUES(form_year),
    `year_month` = COALESCE(VALUES(`year_month`), `year_month`),
    mileage      = VALUES(mileage),
    fuel_name    = VALUES(fuel_name),
    transmission = VALUES(transmission),
    color_name   = COALESCE(VALUES(color_name), color_name),
    price        = VALUES(price),
    is_duplicate = VALUES(is_duplicate),
    last_seen_at = VALUES(last_seen_at),
    -- Seen again => active again. A relisted car must lose its SOLD flag, and
    -- a listing that merely blinked out for one run must lose its streak.
    status           = 'ACTIVE',
    missing_streak   = 0,
    sold_detected_at = NULL,
    last_batch_id    = VALUES(last_batch_id)
"""

# Anything for this model the batch did not refresh was absent from Encar.
# One absence is not a sale, so it only bumps the streak (see migration 005).
BUMP_MISSING_SQL = """
UPDATE vehicles
   SET missing_streak = missing_streak + 1, last_missing_at = %s
 WHERE model_key = %s
   AND status <> 'SOLD'
   AND (last_batch_id IS NULL OR last_batch_id <> %s)
"""

# Rows that just reached the threshold. Rows still under it stay ACTIVE and get
# another chance tomorrow; rows seen again had their streak reset to 0 above.
MARK_SOLD_SQL = """
UPDATE vehicles
   SET status = 'SOLD', sold_detected_at = %s
 WHERE model_key = %s
   AND status <> 'SOLD'
   AND missing_streak >= %s
"""


def as_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ── Row shaping ───────────────────────────────────────────────────────────────
def listing_row(item: Dict[str, Any], model_key: str) -> Dict[str, Any]:
    """The fields the listing API already gives us."""
    year = str(item.get("Year") or "")
    return {
        "car_id": str(item.get("Id")),
        "vehicle_no": None,
        "model_key": model_key,
        "model_info": listing_model_info(item),
        "manufacturer": item.get("Manufacturer"),
        "model_group": None,
        "model_name": item.get("Model"),
        "grade_name": item.get("Badge"),
        "form_year": item.get("FormYear"),
        "year_month": year.split(".")[0] or None,
        "mileage": as_int(item.get("Mileage")),
        "fuel_name": item.get("FuelType"),
        "transmission": item.get("Transmission"),
        "color_name": None,
        "price": as_int(item.get("Price")),
        "is_duplicate": 1 if item.get("ServiceCopyCar") == "DUPLICATION" else 0,
    }


def apply_detail(row: Dict[str, Any], detail: Dict[str, Any]) -> None:
    """Overlay the detail payload — this is where the car number comes from."""
    vehicle_no = detail.get("vehicleNo")
    if vehicle_no:
        row["vehicle_no"] = str(vehicle_no)

    category = detail.get("category") or {}
    spec = detail.get("spec") or {}

    for key, value in (
        ("manufacturer", category.get("manufacturerName")),
        ("model_group", category.get("modelGroupName")),
        ("model_name", category.get("modelName")),
        ("grade_name", category.get("gradeName")),
        ("form_year", category.get("formYear")),
        ("year_month", category.get("yearMonth")),
        ("color_name", spec.get("colorName")),
        ("fuel_name", spec.get("fuelName")),
        ("transmission", spec.get("transmissionName")),
    ):
        if value:
            row[key] = value

    mileage = as_int(spec.get("mileage"))
    if mileage is not None:
        row["mileage"] = mileage

    row["model_info"] = detail_model_info(detail) or row["model_info"]


# ── Crawl ─────────────────────────────────────────────────────────────────────
def crawl_listing_pages(spec: Dict[str, str]) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """-> (rows, total_count, total_pages, fetched_pages)"""
    query = model_query(spec)
    first = fetch_listing_page(query, 0, PAGE_SIZE, want_count=True)
    total_count = int(first.get("Count") or 0)
    total_pages = max(1, math.ceil(total_count / PAGE_SIZE)) if total_count else 0

    rows: Dict[str, Dict[str, Any]] = {}
    for item in first.get("SearchResults") or []:
        row = listing_row(item, spec["key"])
        rows[row["car_id"]] = row
    fetched_pages = 1 if total_pages else 0

    if total_pages > 1:
        def work(offset: int) -> Optional[List[Dict[str, Any]]]:
            try:
                page = fetch_listing_page(query, offset, PAGE_SIZE, want_count=False)
            except RuntimeError as exc:
                log(f"    ! page offset={offset} failed: {exc}")
                return None
            return page.get("SearchResults") or []

        offsets = [p * PAGE_SIZE for p in range(1, total_pages)]
        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            for items in pool.map(work, offsets):
                if items is None:
                    continue
                fetched_pages += 1
                for item in items:
                    row = listing_row(item, spec["key"])
                    rows[row["car_id"]] = row

    return list(rows.values()), total_count, total_pages, fetched_pages


def fill_vehicle_details(rows: List[Dict[str, Any]]) -> int:
    """Fill the car number + detail fields. Returns how many plates we got."""
    def work(row: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        return row, fetch_vehicle_detail(row["car_id"])

    filled = 0
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for row, detail in pool.map(work, rows):
            if not detail:
                continue
            apply_detail(row, detail)
            if row["vehicle_no"]:
                filled += 1
    return filled


# ── Store ─────────────────────────────────────────────────────────────────────
def upsert_vehicles(cur, rows: List[Dict[str, Any]], batch_id: Optional[int],
                    seen_at: datetime) -> None:
    if not rows:
        return
    cur.executemany(UPSERT_SQL, [dict(r, batch_id=batch_id, seen_at=seen_at) for r in rows])


def mark_missing_sold(cur, model_key: str, batch_id: int,
                      detected_at: datetime) -> Tuple[int, int]:
    """-> (listings that were missing this run, listings flipped to SOLD)"""
    cur.execute(BUMP_MISSING_SQL, (detected_at, model_key, batch_id))
    missing = int(cur.rowcount)
    cur.execute(MARK_SOLD_SQL, (detected_at, model_key, SOLD_MISSING_STREAK))
    return missing, int(cur.rowcount)


# ── Run ───────────────────────────────────────────────────────────────────────
def run_model(conn, spec: Dict[str, str], with_details: bool,
              detect_sold: bool) -> Dict[str, Any]:
    log(f"\n=== {spec['label']} ({spec['key']}) ===")
    started = time.perf_counter()
    seen_at = datetime.now()

    cur = conn.cursor() if conn else None
    batch_id: Optional[int] = None
    if cur:
        batch_id = open_batch(cur, spec["key"], seen_at)
        conn.commit()
        log(f"  batch #{batch_id}")

    try:
        rows, total_count, total_pages, fetched_pages = crawl_listing_pages(spec)
        log(f"  list: {len(rows)} listings | {fetched_pages}/{total_pages} pages "
            f"| Encar reports {total_count}")

        filled = 0
        if with_details and rows:
            log(f"  details: fetching {len(rows)} vehicle(s) ...")
            filled = fill_vehicle_details(rows)
            log(f"  details: {filled}/{len(rows)} car numbers")
    except BaseException as exc:
        # A crashed run must not leave its batch sitting at RUNNING forever —
        # that would make the log claim a crawl is still in flight.
        if cur and batch_id is not None:
            fail_batch(conn, cur, batch_id, f"{type(exc).__name__}: {exc}", log)
            log(f"  batch #{batch_id} closed as FAILED")
        raise

    # Shaped the way guards.evaluate_batch() expects, so the sold-detection gate
    # here is literally the same gate the sync run uses.
    metrics = {
        "rows": rows,
        "total_expected_count": total_count,
        "total_expected_pages": total_pages,
        "fetched_pages": fetched_pages,
        "completion_rate": round(fetched_pages / total_pages, 4) if total_pages else 0.0,
    }

    summary: Dict[str, Any] = {
        "model_key": spec["key"],
        "batch_id": batch_id,
        "crawled_count": len(rows),
        "total_expected_count": total_count,
        "fetched_pages": fetched_pages,
        "total_expected_pages": total_pages,
        "completion_rate": metrics["completion_rate"],
        "vehicle_no_filled": filled,
        "missing_now": 0,
        "sold_marked": 0,
        "listings": rows,
    }

    if not cur:
        status, _, note = evaluate_batch(metrics, None)
        summary.update(status=status, note=note, elapsed=time.perf_counter() - started)
        log(f"  status {status}" + (f" — {note}" if note else ""))
        return summary

    assert batch_id is not None  # opened together with `cur`
    try:
        status, may_mark, note = evaluate_batch(metrics, previous_ok_count(cur, spec["key"]))
        if not detect_sold:
            may_mark = False
            note = note or "sold detection disabled (--no-sold)"

        upsert_vehicles(cur, rows, batch_id, seen_at)
        log(f"  stored {len(rows)} row(s) in `vehicles`")

        sold = missing = 0
        if may_mark:
            missing, sold = mark_missing_sold(cur, spec["key"], batch_id, seen_at)
            log(f"  status OK — {missing} listing(s) missing this run, "
                f"{sold} reached {SOLD_MISSING_STREAK} miss(es) and are now SOLD")
        else:
            log(f"  status {status} — sold detection skipped: {note}")

        close_batch(cur, batch_id, metrics, status, may_mark, sold, note)
        conn.commit()
    except BaseException as exc:
        fail_batch(conn, cur, batch_id, f"{type(exc).__name__}: {exc}", log)
        log(f"  batch #{batch_id} closed as FAILED")
        raise

    summary.update(status=status, note=note, sold_marked=sold, missing_now=missing,
                   elapsed=time.perf_counter() - started)
    return summary


def print_summary(summaries: List[Dict[str, Any]], elapsed: float) -> None:
    log("\n" + "=" * 96)
    log(f"{'MODEL':<24}{'STATUS':<9}{'CRAWLED':<9}{'CAR NO.':<9}{'MISSING':<9}"
        f"{'SOLD':<7}{'TIME':<10}NOTE")
    log("-" * 96)
    for s in summaries:
        log(f"{s['model_key']:<24}{s.get('status', '-'):<9}{s['crawled_count']:<9}"
            f"{s['vehicle_no_filled']:<9}{s['missing_now']:<9}{s['sold_marked']:<7}"
            f"{fmt_duration(s['elapsed']):<10}{s.get('note', '')}")
    log("=" * 96)
    log(f"Total: {fmt_duration(elapsed)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl the current Encar listings for each model into `vehicles`."
    )
    parser.add_argument("--models-file", default=MODELS_FILE)
    parser.add_argument("--model", action="append", default=[],
                        help="Model key to crawl (repeatable). Default: every model in models.json")
    parser.add_argument("--no-details", dest="details", action="store_false",
                        help="Skip the per-vehicle detail pass — leaves the car number empty")
    parser.add_argument("--no-sold", dest="detect_sold", action="store_false",
                        help="Store the listings only — do not flag missing ones as SOLD")
    parser.add_argument("--no-db", action="store_true", help="Crawl only, no DB writes")
    parser.add_argument("--json", dest="json_out", help="Write the run summary to this JSON file")
    parser.add_argument("--csv", dest="csv_out", help="Write one summary row per model here")
    parser.add_argument("--out-dir", nargs="?", const=DEFAULT_OUT_DIR,
                        help=f"Write both, named <run>_<timestamp> in this directory "
                             f"(default: {os.path.relpath(DEFAULT_OUT_DIR, ROOT_DIR)})")
    parser.add_argument("--listings", action="store_true",
                        help="Include every crawled listing in the JSON (large)")
    parser.add_argument("--log-file", default=os.getenv("LOG_FILE"),
                        help="Also append the run log here, rotating (default: $LOG_FILE)")
    parser.add_argument("--lock-timeout", type=int, default=int(os.getenv("LOCK_TIMEOUT", "0")),
                        help="Seconds to wait for the run lock before giving up (default 0)")
    args = parser.parse_args()

    if args.log_file:
        resolved = setup_file_log(args.log_file)
        log(f"--- run started {datetime.now().isoformat(timespec='seconds')} "
            f"(log: {resolved})")

    specs = load_specs(args.models_file, args.model)
    conn = None if args.no_db else connect()

    name = lock_name(LOCK_PREFIX)
    if conn and not acquire_lock(conn, name, args.lock_timeout):
        conn.close()
        # Yesterday's run is still going. Two concurrent runs would open rival
        # batches and mark each other's listings missing.
        log(f"Another run holds {name!r} — exiting without crawling.")
        return 2

    started = time.perf_counter()
    summaries: List[Dict[str, Any]] = []
    failed = False
    try:
        for i, spec in enumerate(specs):
            if i and MODEL_COOLDOWN:
                log(f"\n  cooling down {MODEL_COOLDOWN:.0f}s before {spec['key']} "
                    f"(SYNC_MODEL_COOLDOWN) ...")
                time.sleep(MODEL_COOLDOWN)
            try:
                summaries.append(run_model(conn, spec, args.details, args.detect_sold))
            except Exception as exc:  # one bad model must not kill the run
                failed = True
                log(f"  !! {spec['key']} failed: {type(exc).__name__}: {exc}")
                if conn:
                    conn.rollback()
    finally:
        if conn:
            release_lock(conn, name)
            conn.close()

    elapsed = time.perf_counter() - started
    print_summary(summaries, elapsed)

    if args.json_out or args.csv_out or args.out_dir:
        rows = [{k: v for k, v in s.items() if k != "listings" or args.listings}
                for s in summaries]
        for path in results.save(
            TOOL, "run", rows,
            {"models": len(summaries), "elapsedSeconds": round(elapsed, 2)},
            json_path=args.json_out, csv_path=args.csv_out, out_dir=args.out_dir,
        ):
            log(f"Wrote {path}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
