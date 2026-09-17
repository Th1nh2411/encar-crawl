#!/usr/bin/env python3
"""
trigger_collector.py -- Trigger the Bright Data collector and save what it returns.

    python brightdata_collector/trigger_collector.py                  # 10 pages
    python brightdata_collector/trigger_collector.py --max-page 3
    python brightdata_collector/trigger_collector.py --timeout 900
    python brightdata_collector/trigger_collector.py --collection-id c_xxx

This is the same call as the documented curl:

    POST api.brightdata.com/dca/trigger?collector=<id>&queue_next=1
         [{"url": "<search page>", "max_page": 10}]

and then it polls for the result instead of leaving you to check by hand.

The token comes from API_TOKEN_BRINGDATA in the project-root .env -- the same
one the zone calls use. Results land in output/brightdata_collector/ as both
.json and .csv, named after the model and the time so runs do not overwrite
each other -- the same document shape listing_export and stock_pipeline write
(see results.py). The collector's untouched rows are kept in the JSON under
`collectorRows`, so nothing it returned is lost to the row mapping.

The search URL is built by the same code listing_export uses, which matters: the
page's router only accepts its own spelling of the `#!{...}` hash -- no spaces
after ':' or ',', parentheses left literal. A hash built with plain
json.dumps() loads the page and silently never runs a search.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

import requests

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)

TOOL = "brightdata_collector"
DEFAULT_OUT_DIR = os.path.join(ROOT_DIR, "output", TOOL)

sys.path.insert(0, PKG_DIR)
import results  # noqa: E402

# listing_export owns the query and URL building; no reason to have a second
# copy here that can drift from it.
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "listing_export"))
from listing_export.export_listings import model_query, load_model_spec  # noqa: E402
from listing_export.browser_source import search_page_url  # noqa: E402

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

load_dotenv(os.path.join(ROOT_DIR, ".env"))

TRIGGER_URL = "https://api.brightdata.com/dca/trigger"
DATASET_URL = "https://api.brightdata.com/dca/dataset"

DEFAULT_COLLECTOR = os.getenv("BRIGHTDATA_COLLECTOR", "c_mu3w82kc1eic69urfu")
DEFAULT_MODEL = "bmw_3series_g20"
DEFAULT_MAX_PAGE = 10

# The collector runs on Bright Data's side, so polling is the only way to know
# it finished. It answers 202 while the job is still going.
POLL_INTERVAL = 10.0
DEFAULT_TIMEOUT = 600.0


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def api_token() -> str:
    value = (os.getenv("API_TOKEN_BRINGDATA") or "").strip()
    if not value:
        raise SystemExit("API_TOKEN_BRINGDATA is not set in .env")
    return value


def make_session() -> requests.Session:
    s = requests.Session()
    # macOS system proxy settings otherwise leak in and break these calls.
    s.trust_env = False
    s.headers.update({
        "Authorization": f"Bearer {api_token()}",
        "Content-Type": "application/json",
    })
    return s


def trigger_run(s: requests.Session, collector: str, url: str,
            max_page: int, override_schema: bool = False) -> str:
    """Start a run and return its collection id.

    A collector whose fields do not line up with the output schema it is linked
    to is refused with 422 `output_schema_incompatible`, and Bright Data's own
    remedy is `override_incompatible_schema=1`. That is retried automatically
    here, but it is worth fixing properly in the IDE: the override lets the run
    proceed while silently dropping whatever does not fit.
    """
    params: Dict[str, Any] = {"collector": collector, "queue_next": 1}
    if override_schema:
        params["override_incompatible_schema"] = 1

    resp = s.post(
        TRIGGER_URL, params=params,
        json=[{"url": url, "max_page": max_page}], timeout=60,
    )

    if resp.status_code == 422 and not override_schema:
        if "output_schema_incompatible" in resp.text:
            log("  collector fields do not match its output schema; "
                "retrying with override_incompatible_schema=1")
            log("  (fix the schema in the IDE -- the override drops fields "
                "that do not fit)")
            return trigger_run(s, collector, url, max_page, override_schema=True)

    if resp.status_code != 200:
        raise SystemExit(f"Trigger failed: HTTP {resp.status_code} {resp.text[:300]}")

    data = resp.json()
    # The field has been spelled both ways across Bright Data's docs; take
    # whichever is present rather than guessing.
    for key in ("collection_id", "response_id", "id"):
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
    raise SystemExit(f"No collection id in trigger response: {json.dumps(data)[:300]}")


def parse_result_body(text: str):
    """Parse a result body, which is NDJSON as often as it is JSON.

    Finished runs come back as one JSON object per line rather than as an
    array, so `resp.json()` alone fails on exactly the responses that carry
    the data. Both shapes are accepted; None means neither parsed.
    """
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
            return None  # not NDJSON either -- let the caller show the body
    return rows


def wait_for_rows(s: requests.Session, collection_id: str,
         timeout: float) -> List[Dict[str, Any]]:
    """Wait for the run to finish and return its rows.

    202 means still running. A 200 with an empty list means it finished and
    found nothing -- that is a result, not an error, so it is returned as is.
    """
    started = time.time()
    while True:
        resp = s.get(DATASET_URL, params={"id": collection_id}, timeout=60)

        if resp.status_code == 200:
            data = parse_result_body(resp.text)
            if data is None:
                raise SystemExit(f"Could not read the result: {resp.text[:300]}")
            if isinstance(data, dict):
                # Some runs come back wrapped; unwrap the obvious shapes.
                for key in ("data", "results", "items"):
                    if isinstance(data.get(key), list):
                        return data[key]
                # A dict that reports it is still running is not a result yet.
                if str(data.get("status", "")).lower() in ("running", "pending", "building"):
                    data = None
                else:
                    return [data]
            elif isinstance(data, list):
                return data
        elif resp.status_code not in (202, 404):
            raise SystemExit(f"Result fetch failed: HTTP {resp.status_code} "
                             f"{resp.text[:300]}")

        waited = time.time() - started
        if waited > timeout:
            raise SystemExit(
                f"Still running after {timeout:.0f}s. The run is not lost -- "
                f"fetch it later with --collection-id {collection_id}"
            )
        log(f"  ... running ({waited:.0f}s)")
        time.sleep(POLL_INTERVAL)


def to_rows(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The two requested fields, plus the source URL for traceability."""
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append({
            "car_number": item.get("car_number"),
            "model_info": item.get("model_info"),
            "product_page_url": item.get("product_page_url"),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trigger the Bright Data collector and save its results.")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"models.json key (default: {DEFAULT_MODEL})")
    parser.add_argument("--collector", default=DEFAULT_COLLECTOR,
                        help=f"collector id (default: {DEFAULT_COLLECTOR})")
    parser.add_argument("--max-page", type=int, default=DEFAULT_MAX_PAGE,
                        help=f"pages for the collector to walk "
                             f"(default: {DEFAULT_MAX_PAGE})")
    parser.add_argument("--limit", type=int, default=20,
                        help="cars per page in the search URL (default: 20)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"seconds to wait for the run "
                             f"(default: {DEFAULT_TIMEOUT:.0f})")
    parser.add_argument("--collection-id",
                        help="skip the trigger and fetch this existing run")
    parser.add_argument("--url", help="override the search URL entirely")
    parser.add_argument("--json", dest="json_out",
                        help="write the run to this JSON file")
    parser.add_argument("--csv", dest="csv_out",
                        help="write the rows to this CSV file")
    parser.add_argument("--out-dir", nargs="?", const=DEFAULT_OUT_DIR,
                        default=DEFAULT_OUT_DIR,
                        help=f"write both, named <model>_<timestamp> in this "
                             f"directory (default: "
                             f"{os.path.relpath(DEFAULT_OUT_DIR, ROOT_DIR)})")
    parser.add_argument("--quiet", action="store_true",
                        help="do not print the JSON document to stdout")
    args = parser.parse_args()

    s = make_session()

    if args.collection_id:
        collection_id = args.collection_id
        log(f"Fetching existing run {collection_id}")
    else:
        spec = load_model_spec(args.model)
        url = args.url or search_page_url(model_query(spec), 1, args.limit)
        log(f"Model {args.model} ({spec.get('label', '?')}), "
            f"collector {args.collector}, max_page {args.max_page}")
        collection_id = trigger_run(s, args.collector, url, args.max_page)
        log(f"  triggered: {collection_id}")

    raw = wait_for_rows(s, collection_id, args.timeout)
    rows = to_rows(raw)
    filled = sum(1 for r in rows if r["car_number"])
    log(f"  {filled}/{len(rows)} rows with a car number")

    meta = {
        "collectionId": collection_id,
        "collector": args.collector,
        "maxPage": args.max_page,
        "carNumbersFilled": filled,
        # What the collector actually returned, untouched -- to_rows() keeps
        # three fields, and a collector schema change must not silently lose
        # the rest of a paid run.
        "collectorRows": raw,
    }

    for path in results.save(TOOL, args.model, rows, meta, json_path=args.json_out,
                             csv_path=args.csv_out, out_dir=args.out_dir):
        log(f"  wrote {path}")

    if not args.quiet:
        json.dump(results.envelope(TOOL, args.model, rows,
                                   {k: v for k, v in meta.items()
                                    if k != "collectorRows"}),
                  sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
