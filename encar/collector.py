"""
collector.py -- The hosted Bright Data collector: trigger a run, wait for rows.

    POST api.brightdata.com/dca/trigger?collector=<id>&queue_next=1
         [{"url": "<search page>", "max_page": 10}]
    GET  api.brightdata.com/dca/dataset?id=<collection id>     (until it is done)

The collector's own code -- the page interaction and parser scripts it runs in
Bright Data's IDE -- is kept in collector/ at the project root. It renders
every list and detail page in a real browser and returns car number, model
info and the detail URL per car; the `collector` route (routes.py) turns that
into the shared row shape.

The search URL is built by browser.search_page_url, which matters: the page's
router only accepts its own spelling of the `#!{...}` hash. A hash built with
plain json.dumps() loads the page and silently never runs a search.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import requests

from .log import log

TRIGGER_URL = "https://api.brightdata.com/dca/trigger"
DATASET_URL = "https://api.brightdata.com/dca/dataset"

DEFAULT_COLLECTOR = os.getenv("BRIGHTDATA_COLLECTOR") or "c_mu3w82kc1eic69urfu"

# The collector runs on Bright Data's side, so polling is the only way to know
# it finished. It answers 202 while the job is still going. Poll tight at first
# so a short run is not padded by a fixed sleep, then back off.
POLL_START = 2.0
POLL_MAX = 10.0
POLL_GROWTH = 1.5


def api_token() -> Optional[str]:
    return (os.getenv("API_TOKEN_BRINGDATA") or "").strip() or None


def make_session() -> requests.Session:
    token = api_token()
    if not token:
        raise RuntimeError("API_TOKEN_BRINGDATA is not set in .env")
    s = requests.Session()
    # macOS system proxy settings otherwise leak in and break these calls.
    s.trust_env = False
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    return s


def trigger_run(s: requests.Session, url: str, max_page: int,
                collector: str = DEFAULT_COLLECTOR,
                override_schema: bool = False) -> str:
    """Start a run and return its collection id.

    A collector whose fields do not line up with its output schema is refused
    with 422 `output_schema_incompatible`, and Bright Data's own remedy is
    `override_incompatible_schema=1`. That is retried automatically here, but
    it is worth fixing in the IDE: the override silently drops what does not fit.
    """
    params: Dict[str, Any] = {"collector": collector, "queue_next": 1}
    if override_schema:
        params["override_incompatible_schema"] = 1

    resp = s.post(TRIGGER_URL, params=params,
                  json=[{"url": url, "max_page": max_page}], timeout=60)

    if (resp.status_code == 422 and not override_schema
            and "output_schema_incompatible" in resp.text):
        log("  collector fields do not match its output schema; "
            "retrying with override_incompatible_schema=1")
        return trigger_run(s, url, max_page, collector, override_schema=True)

    if resp.status_code != 200:
        raise RuntimeError(f"Trigger failed: HTTP {resp.status_code} {resp.text[:300]}")

    data = resp.json()
    # The field has been spelled several ways across Bright Data's docs.
    for key in ("collection_id", "response_id", "id"):
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
    raise RuntimeError(f"No collection id in trigger response: {json.dumps(data)[:300]}")


def parse_result_body(text: str):
    """A result body is NDJSON as often as it is JSON; None means neither parsed."""
    text = text.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except ValueError:
        pass

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            return None
    return rows


def wait_for_rows(s: requests.Session, collection_id: str,
                  timeout: float) -> List[Dict[str, Any]]:
    """Wait for the run to finish and return its rows.

    202 means still running. A 200 with an empty list means it finished and
    found nothing -- that is a result, not an error.
    """
    started = time.time()
    interval = POLL_START
    while True:
        resp = s.get(DATASET_URL, params={"id": collection_id}, timeout=60)

        if resp.status_code == 200:
            data = parse_result_body(resp.text)
            if data is None:
                raise RuntimeError(f"Could not read the result: {resp.text[:300]}")
            if isinstance(data, dict):
                for key in ("data", "results", "items"):
                    if isinstance(data.get(key), list):
                        return data[key]
                # A dict that reports it is still running is not a result yet.
                if str(data.get("status", "")).lower() not in ("running", "pending",
                                                               "building"):
                    return [data]
            elif isinstance(data, list):
                return data
        elif resp.status_code not in (202, 404):
            raise RuntimeError(f"Result fetch failed: HTTP {resp.status_code} "
                               f"{resp.text[:300]}")

        waited = time.time() - started
        if waited > timeout:
            raise RuntimeError(f"collector run {collection_id} still running after "
                               f"{timeout:.0f}s")
        log(f"  ... collector running ({waited:.0f}s)")
        time.sleep(interval)
        interval = min(interval * POLL_GROWTH, POLL_MAX)
