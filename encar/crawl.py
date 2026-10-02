"""
crawl.py -- One crawl: for each model, get the listings by whatever route
works, store them, and decide what has been sold.

    for each model:
        open a batch                         crawl_batches, status RUNNING
        load the car numbers already known   vehicles -- no request for these
        walk the ladder                      ladder.py -> routes.py
        store every row it brought back      merged into vehicles
        if the run covered the whole model   guards.py
            bump the missing, mark SOLD      after SOLD_MISSING_STREAK misses
        close the batch                      OK / PARTIAL / FAILED

One crawl at a time, enforced with a lock file: two overlapping runs would
each stamp their own batch and mark the other's listings missing.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence

from . import store
from .config import (DETAIL_MAX_AGE_DAYS, LEGACY_PLATE_CACHE, LEGACY_THROTTLE_STATE,
                     LOCK_PATH,
                     MODEL_COOLDOWN, SOLD_MISSING_STREAK)
from .guards import evaluate
from .ladder import (ALL_ROUTES, DEFAULT_MIN_FILL, THROTTLE_COOLDOWN, build_ladder,
                     run_ladder)
from .log import fmt_duration, log
from .models import Spec
from .routes import Options


class Locked(RuntimeError):
    pass


@contextmanager
def crawl_lock(path: str = LOCK_PATH) -> Iterator[None]:
    """Exclusive for the whole crawl; released by the OS even on a crash."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Locked(f"another crawl holds {path}")
        fh.write(str(os.getpid()))
        fh.flush()
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass
class Settings:
    routes: Sequence[str] = ALL_ROUTES
    pages: Optional[List[int]] = None          # None = every page
    detect_sold: bool = True
    refresh_details: bool = False
    detail_max_age: float = DETAIL_MAX_AGE_DAYS
    min_fill: float = DEFAULT_MIN_FILL
    cooldown: float = THROTTLE_COOLDOWN
    race: bool = False
    model_cooldown: float = MODEL_COOLDOWN
    options: Options = field(default_factory=Options)


def seed_from_legacy_cache(conn) -> None:
    """First run after the move to SQLite: take over the old JSON state files."""
    if os.path.exists(LEGACY_PLATE_CACHE):
        added = store.import_plate_cache(conn, LEGACY_PLATE_CACHE)
        os.replace(LEGACY_PLATE_CACHE, LEGACY_PLATE_CACHE + ".imported")
        log(f"  imported {added} car number(s) from "
            f"{os.path.basename(LEGACY_PLATE_CACHE)} (renamed to .imported)")

    if os.path.exists(LEGACY_THROTTLE_STATE):
        try:
            with open(LEGACY_THROTTLE_STATE, encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = {}
        state = store.load_route_state(conn)
        for route, entry in (old if isinstance(old, dict) else {}).items():
            state.setdefault(route, entry)
        store.save_route_state(conn, state)
        os.replace(LEGACY_THROTTLE_STATE, LEGACY_THROTTLE_STATE + ".imported")


def crawl_model(conn, spec: Spec, s: Settings) -> Dict[str, Any]:
    """Crawl one model into the database. Returns its batch summary."""
    key = spec["key"]
    log(f"\n=== {spec.get('label', key)} ({key}) ===")
    started = time.perf_counter()
    batch_id = store.open_batch(conn, key)
    log(f"  batch #{batch_id}")

    attempts: List[Dict[str, str]] = []
    try:
        known = ({} if s.refresh_details
                 else store.known_details(conn, None, s.detail_max_age))
        state = store.load_route_state(conn)
        run, attempts = run_ladder(
            build_ladder(s.routes, spec, s.pages, known, s.options), state,
            min_fill=s.min_fill, cooldown=s.cooldown, race=s.race)
        store.save_route_state(conn, state)

        if run is None:
            raise RuntimeError("every route failed -- Encar is likely rate "
                               "limiting this host; wait a few minutes")

        rows = run["rows"]
        fresh = [r["car_id"] for r in rows if r["car_id"] not in known]
        store.upsert_vehicles(conn, key, rows, batch_id, fresh)
        filled = sum(1 for r in rows if r.get("vehicle_no"))
        log(f"  stored {len(rows)} listing(s), {filled} with a car number")

        status, may_mark, note = evaluate(run, store.previous_ok_count(conn, key))
        if run.get("incomplete"):
            note = "; ".join(n for n in (run["incomplete"], note) if n)
        if not s.detect_sold and may_mark:
            may_mark, note = False, "sold detection disabled (--no-sold)"

        missing = sold = 0
        if may_mark:
            missing, sold = store.mark_missing(conn, key, batch_id, SOLD_MISSING_STREAK)
            log(f"  {missing} listing(s) missing this run, {sold} reached "
                f"{SOLD_MISSING_STREAK} miss(es) and are now SOLD")
        else:
            log(f"  status {status} -- sold detection skipped: {note}")

        requested = run.get("pagesRequested") or 0
        store.close_batch(
            conn, batch_id, status=status, route=run["route"], attempts=attempts,
            every_page=1 if run.get("everyPage") else 0,
            listing_total=run.get("listingTotal"),
            pages_requested=requested, pages_fetched=run.get("pagesFetched"),
            completion_rate=round((run.get("pagesFetched") or 0) / requested, 4)
            if requested else None,
            crawled_count=len(rows), vehicle_no_filled=filled,
            sold_checked=1 if may_mark else 0, missing_count=missing,
            sold_marked_count=sold, note=note or None)
        conn.commit()
    except BaseException as exc:
        # A crashed run must not sit at RUNNING forever.
        store.fail_batch(conn, batch_id, f"{type(exc).__name__}: {exc}", attempts)
        log(f"  batch #{batch_id} closed as FAILED")
        raise

    return {
        "model": key, "batch": batch_id, "status": status, "route": run["route"],
        "rows": len(rows), "filled": filled, "missing": missing, "sold": sold,
        "note": note, "elapsed": time.perf_counter() - started,
    }


def crawl(conn, specs: Sequence[Spec], s: Settings) -> List[Dict[str, Any]]:
    """Crawl every model in turn. One failing model does not stop the rest."""
    summaries: List[Dict[str, Any]] = []
    for i, spec in enumerate(specs):
        if i and s.model_cooldown:
            log(f"\n  cooling down {s.model_cooldown:.0f}s before {spec['key']} "
                f"(ENCAR_MODEL_COOLDOWN)")
            time.sleep(s.model_cooldown)
        try:
            summaries.append(crawl_model(conn, spec, s))
        except Exception as exc:
            log(f"  !! {spec['key']} failed: {type(exc).__name__}: {exc}")
            summaries.append({"model": spec["key"], "status": "FAILED", "route": "-",
                              "rows": 0, "filled": 0, "missing": 0, "sold": 0,
                              "note": str(exc), "elapsed": 0.0})
    print_summary(summaries)
    return summaries


def print_summary(summaries: Sequence[Dict[str, Any]]) -> None:
    log("\n" + "=" * 100)
    log(f"{'MODEL':<20}{'STATUS':<9}{'ROUTE':<17}{'ROWS':<7}{'CAR NO.':<9}"
        f"{'MISSING':<9}{'SOLD':<6}{'TIME':<11}NOTE")
    log("-" * 100)
    for x in summaries:
        log(f"{x['model']:<20}{x['status']:<9}{x['route']:<17}{x['rows']:<7}"
            f"{x['filled']:<9}{x['missing']:<9}{x['sold']:<6}"
            f"{fmt_duration(x['elapsed']):<11}{x.get('note') or ''}")
    log("=" * 100)
