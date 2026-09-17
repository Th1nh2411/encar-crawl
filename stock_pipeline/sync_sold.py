"""
sync_sold.py — Crawl the Encar listings for each model and mark the vehicles
that disappeared from Encar as SOLD in the `stock` table.

This is the *comparison* half of the pipeline. crawl_listings.py owns the
`vehicles` master table and its own missing-streak sold detection; here the
question is only which rows of someone else's `stock` table no longer appear on
Encar at all.

    listing API -> car_id per advertised listing  (1 request per 200 listings)
    `stock` row whose car_id is absent from the batch  ->  SOLD

Matching key = Encar listing id (`car_id`), NOT the vehicle number: the listing
endpoint does not carry `vehicleNo` (see encar_api.py), so matching on car_id
keeps the crawl at ~1 request per 200 listings instead of 1 per vehicle.
`--details` fills the vehicle number anyway, for the record, and never affects
the comparison.

Nothing is marked unless the batch passes guards.evaluate_batch() — completion
rate >= 99%, no zero-row batch, no >50% drop against the previous OK batch. A
skipped batch changes no `stock` rows; the next healthy run does the work.

Usage
-----
    pip install requests pymysql python-dotenv

    # Crawl only, no DB — writes a snapshot (use this to smoke-test)
    python stock_pipeline/sync_sold.py --no-db --out-dir output/sync_sold

    # Full run against the DB, but report the SOLD candidates without writing
    python stock_pipeline/sync_sold.py --dry-run

    # Full run
    python stock_pipeline/sync_sold.py

    # Restrict to some models from models.json
    python stock_pipeline/sync_sold.py --model bmw_3series_g20
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
    MODELS_FILE,
    PAGE_SIZE,
    ROOT_DIR,
    STOCK_CAR_ID_COL,
    STOCK_SOLD_AT_COL,
    STOCK_SOLD_VALUE,
    STOCK_STATUS_COL,
    STOCK_TABLE,
)
from db import (
    close_batch,
    connect,
    ident,
    insert_crawled_vehicles,
    open_batch,
    previous_ok_count,
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
from run_log import fmt_duration, log

TOOL = "sync_sold"
DEFAULT_OUT_DIR = os.path.join(ROOT_DIR, "output", TOOL)


# ── Crawl ─────────────────────────────────────────────────────────────────────
def listing_row(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "car_id": str(item.get("Id")),
        "vehicle_no": None,  # not exposed by the listing API
        "model_info": listing_model_info(item),
        "is_duplicate": 1 if item.get("ServiceCopyCar") == "DUPLICATION" else 0,
    }


def fill_vehicle_numbers(rows: List[Dict[str, Any]]) -> int:
    """Fill `vehicle_no` (and sharpen `model_info`) from the detail endpoint.

    One request per listing. Failures leave `vehicle_no` NULL — they must not
    affect the sold-detection gate, which matches on `car_id`.
    """
    def work(row: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        return row, fetch_vehicle_detail(row["car_id"])

    filled = 0
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for row, detail in pool.map(work, rows):
            if not detail:
                continue
            vehicle_no = detail.get("vehicleNo")
            if vehicle_no:
                row["vehicle_no"] = str(vehicle_no)
                filled += 1
            row["model_info"] = detail_model_info(detail) or row["model_info"]
    return filled


def crawl_model(spec: Dict[str, str], with_details: bool = False) -> Dict[str, Any]:
    """Crawl every listing page for one model. Returns rows + crawl metrics."""
    query = model_query(spec)
    started = time.perf_counter()

    first = fetch_listing_page(query, 0, PAGE_SIZE, want_count=True)
    total_count = int(first.get("Count") or 0)
    total_pages = max(1, math.ceil(total_count / PAGE_SIZE)) if total_count else 0

    rows: Dict[str, Dict[str, Any]] = {}
    for item in first.get("SearchResults") or []:
        row = listing_row(item)
        rows[row["car_id"]] = row

    fetched_pages = 1 if total_pages else 0
    failed_offsets: List[int] = []

    if total_pages > 1:
        offsets = [p * PAGE_SIZE for p in range(1, total_pages)]

        def work(offset: int) -> Tuple[int, Optional[List[Dict[str, Any]]]]:
            try:
                data = fetch_listing_page(query, offset, PAGE_SIZE, want_count=False)
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
                    row = listing_row(item)
                    rows[row["car_id"]] = row

    all_rows = list(rows.values())
    vehicle_no_filled = 0
    if with_details and all_rows:
        log(f"    fetching {len(all_rows)} detail page(s) for vehicle numbers ...")
        vehicle_no_filled = fill_vehicle_numbers(all_rows)
        log(f"    vehicle numbers: {vehicle_no_filled}/{len(all_rows)}")

    return {
        "rows": all_rows,
        "vehicle_no_filled": vehicle_no_filled,
        "total_expected_count": total_count,
        "total_expected_pages": total_pages,
        "fetched_pages": fetched_pages,
        "failed_offsets": failed_offsets,
        "completion_rate": round(fetched_pages / total_pages, 4) if total_pages else 0.0,
        "elapsed": time.perf_counter() - started,
    }


# ── Comparison ────────────────────────────────────────────────────────────────
def mark_missing_stock_sold(cur, batch_id: int, spec: Dict[str, str],
                            dry_run: bool) -> int:
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
        "elapsed": crawl["elapsed"],
        "sold_marked": 0,
        "listings": crawl["rows"],
    }

    if not cur:
        status, _, note = evaluate_batch(crawl, None)
        log(f"  status {status}" + (f" — {note}" if note else ""))
        summary.update(status=status, comparison_ran=False, note=note)
        return summary

    assert batch_id is not None  # opened together with `cur`
    prev = previous_ok_count(cur, spec["key"])
    status, may_compare, note = evaluate_batch(crawl, prev)
    if not compare:
        may_compare = False
        note = note or "comparison disabled (--no-compare)"

    insert_crawled_vehicles(cur, batch_id, crawl["rows"], crawled_at)

    sold_count = 0
    if may_compare:
        sold_count = mark_missing_stock_sold(cur, batch_id, spec, dry_run)
        verb = "would be marked" if dry_run else "marked"
        log(f"  status OK — {sold_count} vehicle(s) {verb} SOLD")
    else:
        log(f"  status {status} — comparison skipped: {note}")

    close_batch(cur, batch_id, crawl, status, may_compare and not dry_run,
                sold_count, note)
    if dry_run:
        conn.rollback()
        log("  dry-run: rolled back")
    else:
        conn.commit()

    summary.update(status=status, comparison_ran=may_compare, sold_marked=sold_count,
                   note=note)
    return summary


def print_summary(summaries: List[Dict[str, Any]], elapsed: float) -> None:
    log("\n" + "=" * 78)
    log(f"{'MODEL':<24}{'STATUS':<10}{'CRAWLED':<10}{'VEH.NO':<10}{'RATE':<10}"
        f"{'SOLD':<8}NOTE")
    log("-" * 78)
    for s in summaries:
        log(f"{s['model_key']:<24}{s['status']:<10}{s['crawled_count']:<10}"
            f"{s['vehicle_no_filled']:<10}{s['completion_rate']:<10.2%}"
            f"{s['sold_marked']:<8}{s.get('note', '')}")
    log("=" * 78)
    log(f"Total: {fmt_duration(elapsed)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Encar listings per model and mark missing stock vehicles as SOLD."
    )
    parser.add_argument("--models-file", default=MODELS_FILE)
    parser.add_argument("--model", action="append", default=[],
                        help="Model key to run (repeatable). Default: every model in models.json")
    parser.add_argument("--no-db", action="store_true",
                        help="Crawl only — no DB writes, no comparison")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything but roll back; reports the SOLD count")
    parser.add_argument("--no-compare", dest="compare", action="store_false",
                        help="Crawl and store only — skip the stock comparison / SOLD marking")
    parser.add_argument("--details", action="store_true",
                        help="Also fetch each listing's detail page to fill in the vehicle "
                             "number (1 extra request per listing)")
    parser.add_argument("--json", dest="json_out", help="Write the run summary to this JSON file")
    parser.add_argument("--csv", dest="csv_out", help="Write one summary row per model here")
    parser.add_argument("--out-dir", nargs="?", const=DEFAULT_OUT_DIR,
                        help=f"Write both, named <run>_<timestamp> in this directory "
                             f"(default: {os.path.relpath(DEFAULT_OUT_DIR, ROOT_DIR)})")
    parser.add_argument("--listings", action="store_true",
                        help="Include every crawled listing in the JSON (large)")
    args = parser.parse_args()

    specs = load_specs(args.models_file, args.model)
    conn = None if args.no_db else connect()

    started = time.perf_counter()
    summaries: List[Dict[str, Any]] = []
    failed = False
    try:
        for spec in specs:
            try:
                summaries.append(
                    run_model(conn, spec, args.dry_run, args.details, args.compare))
            except Exception as exc:  # one bad model must not kill the batch
                failed = True
                log(f"  !! {spec['key']} failed: {type(exc).__name__}: {exc}")
                if conn:
                    conn.rollback()
    finally:
        if conn:
            conn.close()

    elapsed = time.perf_counter() - started
    print_summary(summaries, elapsed)

    if args.json_out or args.csv_out or args.out_dir:
        rows = [{k: v for k, v in s.items() if k != "listings" or args.listings}
                for s in summaries]
        for path in results.save(
            TOOL, "run", rows,
            {"models": len(summaries), "elapsedSeconds": round(elapsed, 2),
             "dryRun": args.dry_run},
            json_path=args.json_out, csv_path=args.csv_out, out_dir=args.out_dir,
        ):
            log(f"Wrote {path}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
