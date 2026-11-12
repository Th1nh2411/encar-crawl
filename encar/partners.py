"""
partners.py -- Tell the partners-stock service which of its Encar cars are sold.

    python main.py sync-sold                 # what the 2-hour schedule runs
    python main.py recheck-sold              # the same schedule, 15 minutes later
    python main.py sync-sold --server TEST   # one server only
    python main.py sync-sold --dry-run       # check, log, but POST nothing
    python main.py sync-sold --car-id 42229086 --dry-run

The servers are config.PARTNERS_SERVERS (QA, TEST, LIVE; comment one out
there to stop calling it). One run is one server.

sync-sold -- report the cars that are gone:

    GET  <server url>/active-car-id     the car ids it still shows as live
    GET  api.encar.com/v1/readside/vehicle/<id>?include=ADVERTISEMENT   per car
    POST <server url>/update-car-id     {"carIds": [...the sold ones]}

recheck-sold -- take back a sold report that was wrong. Encar under
maintenance can answer as if every car were gone; 15 minutes later the cars
reported sold are asked about again, and any that is advertised after all goes
back to active:

    GET  <server url>/pending-sold-car-id     the car ids reported sold
    GET  api.encar.com/...                    per car, as above
    POST <server url>/update-active-car-id    {"carIds": [...the live ones]}

Only the servers in config.PARTNERS_RECHECK_SERVERS are rechecked.

What "sold" means, as measured against the live API: a listing that is still
advertised answers 200 with `advertisement.status = "ADVERTISE"` (one under
contract adds `salesStatus = "CONTRACT"` -- not sold yet). A listing that is no
longer advertised answers 404. Nothing else comes back for it.

A 404 is checked a second time after a short pause before it is reported, and
an error (throttled, timeout, 5xx) is never read as sold: the car is simply
asked about again on the next run. Every run, of either kind, is kept in
`sync_runs` (the dashboard's Sold sync tab); nothing else in the database is
touched.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

from . import store
from .api import DETAIL_URL, Fetcher
from .config import PARTNERS_LOCK_PATH, PARTNERS_RECHECK_SERVERS, PARTNERS_SERVERS, WORKERS
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


def fetch_car_ids(base_url: str, path: str) -> List[str]:
    """`{"success": true, "data": [{"carid": ...}]}` -> the car ids, in order, deduplicated."""
    resp = requests.get(f"{base_url}/{path}", timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"{path} answered success=false: {body}")
    ids = (str(item.get("carid") or "").strip() for item in body.get("data") or [])
    return list(dict.fromkeys(i for i in ids if i))


def post_car_ids(base_url: str, path: str, car_ids: List[str]) -> Any:
    resp = requests.post(f"{base_url}/{path}", json={"carIds": car_ids},
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


def error_note(exc: Exception) -> str:
    """Short enough for the history table: 'HTTP 401 on /api/.../active-car-id'."""
    resp = getattr(exc, "response", None)
    if isinstance(exc, requests.HTTPError) and resp is not None:
        return f"HTTP {resp.status_code} on {urlsplit(resp.url).path}"
    return f"{type(exc).__name__}: {exc}"


class UnknownServer(ValueError):
    pass


def sync_sold(server: str = "QA", car_ids: Optional[List[str]] = None,
              dry_run: bool = False, source: str = "manual") -> Dict[str, Any]:
    """Report the cars that are gone. `car_ids` skips the GET (for testing)."""
    return _recorded("sync", server, PARTNERS_SERVERS, car_ids, dry_run, source)


def recheck_sold(server: str = "QA", car_ids: Optional[List[str]] = None,
                 dry_run: bool = False, source: str = "manual") -> Dict[str, Any]:
    """Put back the cars reported sold that Encar still advertises."""
    return _recorded("recheck", server, PARTNERS_RECHECK_SERVERS, car_ids, dry_run, source)


def _recorded(kind: str, server: str, enabled, car_ids: Optional[List[str]],
              dry_run: bool, source: str) -> Dict[str, Any]:
    """One run against one server, kept in sync_runs whatever its outcome.

    `dry_run` skips the POST; `source` is what the history shows it was
    started by. Raises UnknownServer for a server not enabled for this kind of
    run, and crawl.Locked when another run against the same server -- of
    either kind -- holds its lock.
    """
    if server not in PARTNERS_SERVERS or server not in enabled:
        raise UnknownServer(f"{kind} is not enabled for {server} in encar/config.py")
    base_url = PARTNERS_SERVERS[server]
    started = time.perf_counter()
    with crawl_lock(PARTNERS_LOCK_PATH.format(server=server.lower())):
        run: Dict[str, Any] = {"started_at": store.now(), "kind": kind, "source": source,
                               "target": server, "server": urlsplit(base_url).hostname,
                               "dry_run": int(dry_run), "status": "FAILED"}
        try:
            body = _sync if kind == "sync" else _recheck
            result = body(base_url, car_ids, dry_run, run)
            run["status"] = "OK"
        except Exception as exc:
            run["note"] = error_note(exc)
            raise
        finally:
            run["finished_at"] = store.now()
            try:
                conn = store.connect()
                try:
                    store.save_sync_run(conn, run)
                finally:
                    conn.close()
            except Exception as exc:  # the history is a record, not the job
                log(f"[{kind}-sold] could not save the run: {exc}")

    result["elapsed"] = fmt_duration(time.perf_counter() - started)
    return result


def _post(base_url: str, path: str, car_ids: List[str], dry_run: bool,
          run: Dict[str, Any]) -> Any:
    """Send the car ids, unless dry_run. Records the answer in `run`."""
    if not car_ids:
        return None
    if dry_run:
        log(f"[{run['kind']}-sold] dry run: {path} not called")
        return None
    response = post_car_ids(base_url, path, car_ids)
    run.update(reported=1, update_response=response if isinstance(response, str)
               else json.dumps(response, ensure_ascii=False))
    log(f"[{run['kind']}-sold] {path} <- {response}")
    return response


def _sync(base_url: str, car_ids: Optional[List[str]], dry_run: bool,
          run: Dict[str, Any]) -> Dict[str, Any]:
    """The run itself; fills `run` as it goes, so a failure keeps what was known."""
    log(f"sync-sold: {run['started_at']}")
    log(f"Server: {run['server']}")
    if car_ids is None:
        car_ids = fetch_car_ids(base_url, "active-car-id")
    run["checked_ids"] = car_ids
    log(f"Number of active stock check: {len(car_ids)}")

    states = check_car_ids(car_ids) if car_ids else {}
    sold = [i for i in car_ids if states[i]["state"] == SOLD]
    if sold:
        time.sleep(CONFIRM_PAUSE)
        states.update(check_car_ids(sold))
        sold = [i for i in sold if states[i]["state"] == SOLD]

    errors = [i for i in car_ids if states[i]["state"] == ERROR]
    active = [i for i in car_ids if states[i]["state"] == ACTIVE]
    run.update(active_ids=active, sold_ids=sold, error_ids=errors)
    for i in errors:
        log(f"    ! {i}: {states[i]['note']}")
    log(f"After check with encar, active: {len(active)}")
    if errors:
        log(f"Could not check (asked again next run): {len(errors)}")
    log(f"Will be update to sold: {len(sold)}")
    if sold:
        log(f"listSoldCarId = {sold}")

    response = _post(base_url, "update-car-id", sold, dry_run, run)
    return {
        "server": run["target"],
        "checked": len(car_ids),
        "listSoldCarId": sold,
        "errors": {i: states[i]["note"] for i in errors},
        "results": states,
        "reported": bool(sold) and not dry_run,
        "updateResponse": response,
        "dryRun": dry_run,
    }


def _recheck(base_url: str, car_ids: Optional[List[str]], dry_run: bool,
             run: Dict[str, Any]) -> Dict[str, Any]:
    """The recheck; fills `run` like _sync, with active_ids as the ones put back.

    Only an advertised listing is put back: a 404 stays sold and an error
    proves nothing either way, so both are left as they are.
    """
    log(f"recheck-sold: {run['started_at']}")
    log(f"Server: {run['server']}")
    if car_ids is None:
        car_ids = fetch_car_ids(base_url, "pending-sold-car-id")
    run["checked_ids"] = car_ids
    log(f"Number of pending sold check: {len(car_ids)}")

    states = check_car_ids(car_ids) if car_ids else {}
    active = [i for i in car_ids if states[i]["state"] == ACTIVE]
    sold = [i for i in car_ids if states[i]["state"] == SOLD]
    errors = [i for i in car_ids if states[i]["state"] == ERROR]
    run.update(active_ids=active, sold_ids=sold, error_ids=errors)
    for i in errors:
        log(f"    ! {i}: {states[i]['note']}")
    log(f"After check with encar, still sold: {len(sold)}")
    if errors:
        log(f"Could not check (asked again next run): {len(errors)}")
    log(f"Will be update to active: {len(active)}")
    if active:
        log(f"listActiveCarId = {active}")

    response = _post(base_url, "update-active-car-id", active, dry_run, run)
    return {
        "server": run["target"],
        "checked": len(car_ids),
        "listActiveCarId": active,
        "errors": {i: states[i]["note"] for i in errors},
        "results": states,
        "reported": bool(active) and not dry_run,
        "updateResponse": response,
        "dryRun": dry_run,
    }
