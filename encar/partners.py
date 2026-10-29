"""
partners.py -- Tell the partners-stock service which of its Encar cars are sold.

    python main.py sync-sold                 # what the 10-minute schedule runs
    python main.py sync-sold --dry-run       # check, log, but POST nothing
    python main.py sync-sold --car-id 42229086 --dry-run

One run:

    GET  <PARTNERS_API_URL>/active-car-id     the car ids it still shows as live
    GET  api.encar.com/v1/readside/vehicle/<id>?include=ADVERTISEMENT   per car
    POST <PARTNERS_API_URL>/update-car-id     {"carIds": [...the sold ones]}

What "sold" means, as measured against the live API: a listing that is still
advertised answers 200 with `advertisement.status = "ADVERTISE"` (one under
contract adds `salesStatus = "CONTRACT"` -- not sold yet). A listing that is no
longer advertised answers 404. Nothing else comes back for it.

A 404 is checked a second time after a short pause before it is reported, and
an error (throttled, timeout, 5xx) is never read as sold: the car is simply
asked about again on the next run. Nothing here writes to the database.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

from .api import DETAIL_URL, Fetcher
from .config import PARTNERS_API_URL, PARTNERS_LOCK_PATH, WORKERS
from .crawl import crawl_lock
from .log import fmt_duration, log
from .plates import transports
from .transport import Result

ACTIVE, SOLD, ERROR = "ACTIVE", "SOLD", "ERROR"

# Seconds before a 404 is asked about again. A listing being re-posted or
# edited could conceivably blink out; one sold report too many is not undone.
CONFIRM_PAUSE = 5.0

HTTP_TIMEOUT = 30.0
BLOCKED = ("HTTP 401", "HTTP 402", "HTTP 403", "HTTP 407", "HTTP 429")


def verdict(result: Result) -> str:
    """One detail response -> ACTIVE, SOLD or ERROR."""
    if result.gone:
        return SOLD
    if not result.ok:
        return ERROR
    ad = (result.data or {}).get("advertisement") or {}
    return ACTIVE if ad.get("status") == "ADVERTISE" else SOLD


def fetch_active_car_ids(base_url: str = PARTNERS_API_URL) -> List[str]:
    """The car ids the partners service still shows, in its order, deduplicated."""
    resp = requests.get(f"{base_url}/active-car-id", timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"active-car-id answered success=false: {body}")
    ids = (str(item.get("carid") or "").strip() for item in body.get("data") or [])
    return list(dict.fromkeys(i for i in ids if i))


def report_sold(car_ids: List[str], base_url: str = PARTNERS_API_URL) -> Any:
    resp = requests.post(f"{base_url}/update-car-id", json={"carIds": car_ids},
                         timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return resp.text


def check_car_ids(car_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """car_id -> {"state", "note"}, moving to the next transport when Encar blocks one."""
    out: Dict[str, Dict[str, Any]] = {}
    kinds = transports()
    fetcher = Fetcher(kinds.pop(0), WORKERS)
    try:
        todo = list(car_ids)
        while todo:
            results = fetcher.run([(DETAIL_URL.format(car_id=i), {"include": "ADVERTISEMENT"})
                                   for i in todo])
            for car_id, res in zip(todo, results):
                ad = (res.data or {}).get("advertisement") or {}
                out[car_id] = {"state": verdict(res),
                               "note": res.error or ad.get("salesStatus") or ""}
            blocked = [i for i in todo if out[i]["state"] == ERROR
                       and any(m in out[i]["note"] for m in BLOCKED)]
            if not blocked or not kinds:
                break
            fetcher.close()
            fetcher = Fetcher(kinds.pop(0), WORKERS)
            log(f"    {len(blocked)} blocked -> switching to {fetcher.name}")
            todo = blocked
    finally:
        fetcher.close()
    return out


def sync_sold(car_ids: Optional[List[str]] = None, dry_run: bool = False) -> Dict[str, Any]:
    """One full run. `car_ids` skips the GET (for testing); `dry_run` skips the POST.

    Raises crawl.Locked when another run holds the lock.
    """
    started = time.perf_counter()
    with crawl_lock(PARTNERS_LOCK_PATH):
        log(f"Server: {urlsplit(PARTNERS_API_URL).hostname}")
        if car_ids is None:
            car_ids = fetch_active_car_ids()
        log(f"Number of active stock check: {len(car_ids)}")

        states = check_car_ids(car_ids) if car_ids else {}
        sold = [i for i in car_ids if states[i]["state"] == SOLD]
        if sold:
            time.sleep(CONFIRM_PAUSE)
            again = check_car_ids(sold)
            states.update(again)
            sold = [i for i in sold if states[i]["state"] == SOLD]

        errors = [i for i in car_ids if states[i]["state"] == ERROR]
        for i in errors:
            log(f"    ! {i}: {states[i]['note']}")
        log(f"After check with encar, active: {len(car_ids) - len(sold) - len(errors)}")
        if errors:
            log(f"Could not check (asked again next run): {len(errors)}")
        log(f"Will be update to sold: {len(sold)}")
        if sold:
            log(f"listSoldCarId = {sold}")

        response = None
        if sold and not dry_run:
            response = report_sold(sold)
            log(f"[sync-sold] update-car-id <- {response}")
        elif sold:
            log("[sync-sold] dry run: update-car-id not called")

    return {
        "checked": len(car_ids),
        "listSoldCarId": sold,
        "errors": {i: states[i]["note"] for i in errors},
        "results": states,
        "reported": bool(sold) and not dry_run,
        "updateResponse": response,
        "dryRun": dry_run,
        "elapsed": fmt_duration(time.perf_counter() - started),
    }
