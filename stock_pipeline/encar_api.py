"""
encar_api.py — Every call this pipeline makes to Encar, in one place.

Two endpoints, called directly (no browser):

    GET https://api.encar.com/search/car/list/premium
        ?count=true
        &q=<search expression>
        &sr=|ModifiedDate|<offset>|<limit>

    GET https://api.encar.com/v1/readside/vehicle/<carId>

`Hidden.N` in the expression means "currently advertised". The listing endpoint
does not carry `vehicleNo` — verified against the live API, on both the
`premium` and the `general` tab — so the vehicle number always costs one detail
request per car.

Sessions are per thread and keep-alive + cookies is what avoids HTTP 407;
throttling (403/407/429) drops the session and backs off on its own, much
slower, scale. See config.py for the knobs.
"""

import os
import random
import threading
import time
from typing import Any, Dict, Optional

import requests

from config import (
    BASE_BACKOFF,
    MAX_RETRY,
    REQUEST_TIMEOUT,
    THROTTLE_BACKOFF,
    THROTTLE_STATUSES,
)
from run_log import log

SEARCH_URL = "https://api.encar.com/search/car/list/premium"
DETAIL_URL = "https://api.encar.com/v1/readside/vehicle/{car_id}"
HOME_URL = "https://www.encar.com/"

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

_local = threading.local()


# ── Session ───────────────────────────────────────────────────────────────────
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
            session.get(HOME_URL, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            pass
        _local.session = session
    return session


def reset_session() -> None:
    """Drop this thread's session so the next call re-does the cookie warm-up."""
    session = getattr(_local, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
        _local.session = None


def backoff_for(attempt: int, throttled: bool) -> float:
    """Seconds to wait before the next attempt."""
    if throttled:
        return THROTTLE_BACKOFF * (2 ** attempt) + random.uniform(0, 5)
    return BASE_BACKOFF * (attempt + 1) + random.uniform(0, 1.5)


# ── Queries ───────────────────────────────────────────────────────────────────
def model_query(spec: Dict[str, str]) -> str:
    """Search expression for one model. requests handles the URL-encoding.

    `car_type` is Encar's domestic flag: Y for Korean makes (현대, 기아, 제네시스,
    KG모빌리티), N for imports. A Korean model queried with N returns 0 listings,
    so it is per model; the default keeps the original import entries unchanged.
    Encar escapes ')' inside names as '_)' — write names that way in models.json.
    """
    return (
        "(And.Hidden.N._.(C.CarType.{car_type}._.(C.Manufacturer.{manufacturer}._."
        "(C.ModelGroup.{model_group}._.Model.{model}.))))"
    ).format(**{"car_type": "N", **spec})


def plate_query(plate: str, hidden: str = "N") -> str:
    """Search expression for one vehicle number. hidden='N': on sale, 'Y': hidden."""
    return f"(And.Hidden.{hidden}._.CarType.N._.Simple.keyword({plate}).)"


# ── Requests ──────────────────────────────────────────────────────────────────
def fetch_listing_page(query: str, offset: int, limit: int,
                       want_count: bool) -> Dict[str, Any]:
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


def fetch_vehicle_detail(car_id: str) -> Optional[Dict[str, Any]]:
    """Per-vehicle detail. None when the listing is gone (404) or every retry failed."""
    last_err: Optional[str] = None
    throttled = False

    for attempt in range(MAX_RETRY):
        try:
            resp = get_session().get(DETAIL_URL.format(car_id=car_id),
                                     timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            throttled = False
        else:
            if resp.status_code == 200:
                return resp.json() if resp.content else None
            if resp.status_code == 404:  # pulled between the list and detail call
                return None
            last_err = f"HTTP {resp.status_code}"
            throttled = resp.status_code in THROTTLE_STATUSES
            if throttled:
                reset_session()

        if attempt < MAX_RETRY - 1:
            time.sleep(backoff_for(attempt, throttled))

    log(f"    ! detail {car_id} failed: {last_err}")
    return None


# ── Payload helpers ───────────────────────────────────────────────────────────
def listing_model_info(item: Dict[str, Any]) -> Optional[str]:
    """Readable model string from a listing row: make + model + badge."""
    joined = " ".join(
        str(p) for p in (item.get("Manufacturer"), item.get("Model"), item.get("Badge"))
        if p
    ).strip()
    return joined or None


def detail_model_info(detail: Dict[str, Any]) -> Optional[str]:
    """The sharper model string only the detail endpoint carries (grade included)."""
    category = detail.get("category") or {}
    joined = " ".join(
        str(p) for p in (
            category.get("manufacturerName"),
            category.get("modelName"),
            category.get("gradeName"),
            category.get("gradeDetailName"),
        ) if p
    ).strip()
    return joined or None
