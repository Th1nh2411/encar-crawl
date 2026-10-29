"""
cli.py -- `python main.py <command>`.

    crawl        listings -> database, by whatever route works; sold detection
    export       database -> JSON + CSV (cars by plate, or raw listings)
    check-plate  live "is this plate on sale" lookup, shown next to the database
    import-stock sonokong dumps in data/ -> database
    sync-sold    partners-stock car ids -> Encar -> report the sold ones back
    dashboard    the web UI over the database

With no command, `crawl` is assumed: `python main.py --model x` still works.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import List, Optional

from .config import DATA_DIR, DB_PATH, ROOT_DIR
from .log import log, setup_file_log

COMMANDS = ("crawl", "export", "check-plate", "import-stock", "sync-sold", "dashboard")


def parse_pages(text: str) -> Optional[List[int]]:
    """`--pages` takes "3", "2-5" or "all"; 1-based like the site. None = all."""
    text = text.strip().lower()
    if text == "all":
        return None
    try:
        if "-" in text:
            lo, hi = (int(x) for x in text.split("-", 1))
        else:
            lo = hi = int(text)
    except ValueError:
        raise SystemExit(f"Bad --pages value: {text!r} (try 1, 2-5 or all)")
    if lo < 1 or hi < lo:
        raise SystemExit(f"Bad page range: {text!r} (pages are 1-based)")
    return list(range(lo, hi + 1))


# ── crawl ─────────────────────────────────────────────────────────────────────

def cmd_crawl(args) -> int:
    from . import store
    from .crawl import Locked, Settings, crawl, crawl_lock, seed_from_legacy_cache
    from .ladder import ALL_ROUTES
    from .models import load_specs
    from .routes import Options

    if args.log_file:
        log(f"--- crawl started {datetime.now().isoformat(timespec='seconds')} "
            f"(log: {setup_file_log(args.log_file)})")

    routes = [s.strip() for s in args.routes.split(",") if s.strip()]
    unknown = [r for r in routes if r not in ALL_ROUTES]
    if unknown:
        raise SystemExit(f"Unknown route: {', '.join(unknown)}. Known: {', '.join(ALL_ROUTES)}")

    specs = load_specs(args.model)
    settings = Settings(
        routes=routes, pages=parse_pages(args.pages), detect_sold=args.sold,
        refresh_details=args.refresh_details, detail_max_age=args.detail_max_age,
        min_fill=args.min_fill, cooldown=args.cooldown, race=args.race,
        model_cooldown=args.model_cooldown,
        options=Options(workers=args.workers, delay=args.delay,
                        collector_max_page=args.collector_max_page,
                        collector_timeout=args.timeout),
    )
    if settings.pages is not None and settings.detect_sold:
        log("  note: --pages covers part of the model, so sold detection will not run")

    try:
        with crawl_lock():
            conn = store.connect(args.db)
            try:
                # Only into the real database: the old cache is renamed once
                # taken over, and a scratch --db must not consume it.
                if os.path.abspath(args.db) == os.path.abspath(DB_PATH):
                    seed_from_legacy_cache(conn)
                summaries = crawl(conn, specs, settings)
            finally:
                conn.close()
    except Locked as exc:
        log(f"{exc} -- exiting without crawling.")
        return 2
    return 1 if any(s["status"] == "FAILED" for s in summaries) else 0


# ── export ────────────────────────────────────────────────────────────────────

def cmd_export(args) -> int:
    import json

    from . import store
    from .export import CAR_FIELDS, LISTING_FIELDS, envelope, query_rows, save

    conn = store.connect(args.db)
    try:
        rows = query_rows(conn, args.view, args.model, args.status)
    finally:
        conn.close()

    name = args.model or "all"
    meta = {"model": args.model, "status": args.status, "database": args.db}
    fields = CAR_FIELDS if args.view == "cars" else LISTING_FIELDS
    for path in save(args.view, name, rows, meta, json_path=args.json_out,
                     csv_path=args.csv_out, out_dir=args.out_dir, fields=fields):
        log(f"  wrote {path} ({len(rows)} rows)")
    if args.print_json:
        json.dump(envelope(args.view, name, rows, meta), sys.stdout,
                  ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


# ── check-plate ───────────────────────────────────────────────────────────────

def cmd_check_plate(args) -> int:
    from . import store
    from .export import save
    from .plates import CSV_FIELDS, check_plates, csv_rows, normalize_plate, print_table

    raw = list(args.plates)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            raw.extend(line for line in fh.read().splitlines() if line.strip())
    plates = list(dict.fromkeys(p for p in (normalize_plate(x) for x in raw) if p))
    if not plates:
        raise SystemExit("No plates given (pass them, or --file)")

    conn = store.connect(args.db) if os.path.exists(args.db) else None
    started = time.perf_counter()
    try:
        rows = check_plates(plates, args.verify, conn, args.delay)
    finally:
        if conn is not None:
            conn.close()
    print_table(rows, time.perf_counter() - started)

    if args.json_out or args.csv_out or args.out_dir:
        # JSON keeps each plate's ads nested; CSV has no place for a list, so
        # it gets one line per ad.
        for path in save("check_plate", "plates", rows, {"verified": args.verify},
                         json_path=args.json_out, csv_path=args.csv_out,
                         out_dir=args.out_dir, fields=CSV_FIELDS,
                         flat_rows=csv_rows(rows)):
            log(f"  wrote {path}")
    return 0


# ── import-stock / dashboard ──────────────────────────────────────────────────

def cmd_import_stock(args) -> int:
    from . import store
    from .stock_import import import_stock

    conn = store.connect(args.db)
    try:
        import_stock(conn, args.dir, include_deleted=args.all, dry_run=args.dry_run)
    finally:
        conn.close()
    return 0


def cmd_sync_sold(args) -> int:
    from .crawl import Locked
    from .partners import sync_sold

    if args.log_file:
        setup_file_log(args.log_file)
    try:
        result = sync_sold(args.car_ids or None, dry_run=args.dry_run, source="cron")
    except Locked as exc:
        log(f"{exc} -- exiting.")
        return 2
    except Exception as exc:  # the schedule only sees the exit code and the log
        log(f"[sync-sold] FAILED: {type(exc).__name__}: {exc}")
        return 1
    log(f"[sync-sold] done in {result['elapsed']}")
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import run
    run(args.port)
    return 0


# ── parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    from .ladder import ALL_ROUTES, DEFAULT_MIN_FILL, THROTTLE_COOLDOWN
    from .routes import COLLECTOR_MAX_PAGE, COLLECTOR_TIMEOUT
    from .config import DETAIL_MAX_AGE_DAYS, MODEL_COOLDOWN, PARTNERS_LOG_PATH, WORKERS

    rel_db = os.path.relpath(DB_PATH, ROOT_DIR)
    parser = argparse.ArgumentParser(prog="main.py",
                                     description="Encar listings -> one database.")
    sub = parser.add_subparsers(dest="command")

    def db_arg(p):
        p.add_argument("--db", default=DB_PATH, help=f"database file (default: {rel_db})")

    def out_args(p):
        p.add_argument("--json", dest="json_out", help="write JSON here")
        p.add_argument("--csv", dest="csv_out", help="write CSV here")
        p.add_argument("--out-dir", help="write both, named <name>_<timestamp>, here")

    # crawl
    c = sub.add_parser("crawl", help="crawl listings into the database")
    db_arg(c)
    c.add_argument("--model", action="append", default=[],
                   help="models.json key, repeatable (default: every model)")
    c.add_argument("--pages", default="all",
                   help="page, range or all, in the site's pages of 20 (default: all). "
                        "Anything but all skips sold detection")
    c.add_argument("--routes", default=",".join(ALL_ROUTES),
                   help=f"routes to try, in order (default: {','.join(ALL_ROUTES)})")
    c.add_argument("--race", action="store_true",
                   help="when a route fails, start the collector in the background "
                        "instead of waiting to reach it (billed even if unused)")
    c.add_argument("--no-sold", dest="sold", action="store_false",
                   help="store listings only; do not mark missing ones")
    c.add_argument("--refresh-details", action="store_true",
                   help="look up every car number again, even known ones")
    c.add_argument("--detail-max-age", type=float, default=DETAIL_MAX_AGE_DAYS,
                   help=f"days a stored car number is trusted (default: "
                        f"{DETAIL_MAX_AGE_DAYS:.0f}; 0 = forever)")
    c.add_argument("--workers", type=int, default=WORKERS,
                   help=f"requests in flight (default: {WORKERS})")
    c.add_argument("--delay", type=float, default=None,
                   help="seconds between chunks of requests. Left out, worked out "
                        "from the size of the run: none under Encar's ~1200-request "
                        "budget, ~4 req/s above it. 0 disables it")
    c.add_argument("--min-fill", type=float, default=DEFAULT_MIN_FILL,
                   help=f"share of car numbers a route must fill to count "
                        f"(default: {DEFAULT_MIN_FILL})")
    c.add_argument("--cooldown", type=float, default=THROTTLE_COOLDOWN,
                   help=f"skip a route this long after it was throttled; 0 disables "
                        f"(default: {THROTTLE_COOLDOWN:.0f}s)")
    c.add_argument("--model-cooldown", type=float, default=MODEL_COOLDOWN,
                   help=f"pause between models (default: {MODEL_COOLDOWN:.0f}s)")
    c.add_argument("--collector-max-page", type=int, default=COLLECTOR_MAX_PAGE,
                   help=f"pages the collector walks for --pages all "
                        f"(default: {COLLECTOR_MAX_PAGE})")
    c.add_argument("--timeout", type=float, default=COLLECTOR_TIMEOUT,
                   help=f"seconds to wait for the collector (default: {COLLECTOR_TIMEOUT:.0f})")
    c.add_argument("--log-file", default=os.getenv("CRAWL_LOG_FILE"),
                   help="also append the log here (the dashboard reads logs/encar.log)")
    c.set_defaults(func=cmd_crawl)

    # export
    e = sub.add_parser("export", help="write the database out as JSON + CSV")
    db_arg(e)
    e.add_argument("--view", choices=("cars", "listings"), default="cars",
                   help="cars: one row per plate (default); listings: one per ad")
    e.add_argument("--model", help="only this models.json key")
    e.add_argument("--status", help="only this status (cars: ON_SALE, CHECKING, SOLD, "
                                    "NOT_FOUND; listings: ACTIVE, SOLD)")
    out_args(e)
    e.add_argument("--print", dest="print_json", action="store_true",
                   help="also dump the JSON document to stdout")
    e.set_defaults(func=cmd_export)

    # check-plate
    p = sub.add_parser("check-plate", help="live on-sale lookup by plate")
    db_arg(p)
    p.add_argument("plates", nargs="*", help="plates, e.g. 110모5877 269로1841")
    p.add_argument("--file", help="one plate per line")
    p.add_argument("--verify", action="store_true",
                   help="confirm each ad's plate through the detail endpoint (slower)")
    p.add_argument("--delay", type=float, default=0.0, help="seconds between plates")
    out_args(p)
    p.set_defaults(func=cmd_check_plate)

    # import-stock
    i = sub.add_parser("import-stock", help="load the sonokong stock dumps")
    db_arg(i)
    i.add_argument("--dir", default=DATA_DIR, help="where the .sql.gz and .sch files are")
    i.add_argument("--all", action="store_true", help="include soft-deleted stock")
    i.add_argument("--dry-run", action="store_true", help="parse and report only")
    i.set_defaults(func=cmd_import_stock)

    # sync-sold
    s = sub.add_parser("sync-sold", help="report partners-stock cars sold on Encar")
    s.add_argument("--car-id", dest="car_ids", action="append", default=[],
                   help="check this car id instead of asking partners-stock, repeatable")
    s.add_argument("--dry-run", action="store_true",
                   help="check and log, but do not call update-car-id")
    s.add_argument("--log-file", default=PARTNERS_LOG_PATH,
                   help="also append the log here (default: logs/sync-sold.log; "
                        "'' for none)")
    s.set_defaults(func=cmd_sync_sold)

    # dashboard
    d = sub.add_parser("dashboard", help="run the web UI")
    d.add_argument("--port", type=int, default=None, help="default: $UI_PORT or 5050")
    d.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "crawl")
    args = build_parser().parse_args(argv)
    return args.func(args)
