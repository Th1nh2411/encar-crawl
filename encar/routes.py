"""
routes.py -- The five ways to read a model's listings, behind one signature.

    route(spec, pages, known, opts) -> Crawl

Every route reaches the same facts per car -- the listing, and the vehicle
number behind it -- at very different costs, and the cheap ones are the ones
Encar blocks first. ladder.py tries them in order. Measured on a 1130-car model:

    api-direct       1.1s / page   JSON from this host. Free.
    api-scrapingbee  11.9s / page  The same JSON through ScrapingBee's IPs.
    api-brightdata   --            The same through Bright Data: 402 until the
                                   zone passes KYC.
    page-local       12.7s / page  Local Chromium reads the page's own XHR;
                                   car numbers from the detail page HTML.
    collector        85s / page    Hosted Bright Data collector; billed per run.

A Crawl is a dict: `rows` in the shape of parse.py, plus coverage -- what was
asked for against what arrived. Coverage is what ladder.verdict() judges a
route by, and what guards.py gates sold detection on.

`known` is car_id -> stored detail fields. A car in it costs no detail request.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .api import Fetcher, dedupe, expected_cars, fetch_details, fetch_listing
from .config import API_PAGE_SIZE, SECTION, SITE_PAGE_SIZE, WORKERS
from .log import log
from .models import Spec, model_query
from .parse import Row, apply_detail, car_id_from_url, empty_row, listing_row, split_known

Crawl = Dict[str, Any]

# How many pages the collector walks when every page was asked for. Measured:
# 85s for 1 page, 231s for 10 -- most of the 85s is fixed startup, and each
# further page costs ~16s of rendering. A whole model would be a quarter of an
# hour; this is for getting something out while blocked.
COLLECTOR_MAX_PAGE = 10
COLLECTOR_TIMEOUT = 600.0


@dataclass
class Options:
    workers: int = WORKERS
    delay: Optional[float] = None
    collector_max_page: int = COLLECTOR_MAX_PAGE
    collector_timeout: float = COLLECTOR_TIMEOUT


def _fill(rows: List[Row], known: Dict[str, Dict[str, Any]]) -> List[Row]:
    """Apply stored details; return the rows that still need a request."""
    reused, to_fetch = split_known(rows, known)
    if reused:
        log(f"  details: {len(reused)} known, {len(to_fetch)} to fetch")
    return to_fetch


def _result(route: str, transport: str, rows: List[Row],
            coverage: Dict[str, Any]) -> Crawl:
    return {
        "route": route,
        "transport": transport,
        "rows": rows,
        "carNumbersFilled": sum(1 for r in rows if r.get("vehicle_no")),
        **coverage,
    }


# ── JSON API ──────────────────────────────────────────────────────────────────

def api_route(kind: str) -> Callable[..., Crawl]:
    """api-direct / api-scrapingbee / api-brightdata: one transport each."""

    def run(spec: Spec, pages: Optional[List[int]],
            known: Dict[str, Dict[str, Any]], opts: Options) -> Crawl:
        # Every page: 200 cars a request, which is what keeps a cached run to a
        # handful of requests. Specific pages: the site's own 20, so `--pages
        # 3` means the cars on page 3 of the website.
        limit = API_PAGE_SIZE if pages is None else SITE_PAGE_SIZE
        fetcher = Fetcher(kind, opts.workers)
        try:
            rows, coverage = fetch_listing(fetcher, SECTION, model_query(spec),
                                           pages, limit, opts.delay, len(known))
            to_fetch = _fill(rows, known)
            if to_fetch:
                fetch_details(fetcher, to_fetch, opts.delay)
            return _result(f"api-{kind}", fetcher.name, rows, coverage)
        finally:
            fetcher.close()

    return run


# ── Local browser ─────────────────────────────────────────────────────────────

def page_local(spec: Spec, pages: Optional[List[int]],
               known: Dict[str, Dict[str, Any]], opts: Options) -> Crawl:
    """The search page in a local Chromium; details from the detail page HTML.

    The site serves 20 a page whatever is asked for, so this walks pages of 20.
    """
    import time

    from .browser import BrowserSource

    limit = SITE_PAGE_SIZE
    action = model_query(spec)
    source = BrowserSource(workers=opts.workers, log=log)
    try:
        # With every page asked for, page 1 is what reports Count, so the rest
        # is only known once it lands.
        queue = [1] if pages is None else list(pages)
        requested = list(queue)
        rows: List[Row] = []
        failed: List[int] = []
        total: Optional[int] = None

        while queue:
            page_no = queue.pop(0)
            data = source.listing_page(action, page_no, limit, SECTION)
            if data is None:
                log(f"    ! page {page_no}: blocked, no data")
                failed.append(page_no)
                if total is None:
                    # Not even page 1: nothing to go on, and the host is
                    # likely blocked -- the next route is the better bet.
                    raise RuntimeError(f"page {page_no}: blocked, no listing XHR")
                continue

            if total is None:
                total = int(data.get("Count") or 0)
                if pages is None:
                    total_pages = max(1, math.ceil(total / limit)) if total else 1
                    log(f"  {total} listings -> {total_pages} page(s) of {limit}")
                    queue.extend(range(2, total_pages + 1))
                    requested = list(range(1, total_pages + 1))

            items = data.get("SearchResults") or []
            page_rows = [listing_row(it, page_no) for it in items]
            to_fetch = _fill(page_rows, known)
            source.fill_details(to_fetch)
            rows.extend(page_rows)
            log(f"  page {page_no}: {len(items)} cars")

            if opts.delay and queue:
                time.sleep(opts.delay)

        rows = dedupe(rows)
        expected = expected_cars(total or 0, requested, limit) if total else len(rows)
        coverage = {
            "listingTotal": total or 0,
            "everyPage": pages is None,
            "pagesRequested": len(requested),
            "pagesFetched": len(requested) - len(failed),
            "pagesFailed": sorted(failed),
            "expectedCars": expected,
            "missingCars": max(expected - len(rows), 0),
        }
        return _result("page-local", "local-browser", rows, coverage)
    finally:
        source.close()


# ── Hosted collector ──────────────────────────────────────────────────────────

def collector(spec: Spec, pages: Optional[List[int]],
              known: Dict[str, Dict[str, Any]], opts: Options) -> Crawl:
    """The hosted Bright Data collector, reshaped into the shared row shape.

    It walks pages itself from page 1, so pages 3-5 become max_page 5 and the
    extra pages are dropped afterwards. It cannot report how many listings the
    model has, so its runs never count as covering every page -- and therefore
    never drive sold detection.
    """
    from . import collector as dca
    from .browser import search_page_url

    if pages is None:
        max_page = opts.collector_max_page
        log(f"  note: every page falls back to the first {max_page} here -- "
            f"the collector renders each page in a browser")
    else:
        max_page = max(pages)

    session = dca.make_session()
    url = search_page_url(model_query(spec), 1, SITE_PAGE_SIZE)
    collection_id = dca.trigger_run(session, url, max_page)
    log(f"  collector run {collection_id}, max_page {max_page}")
    raw = dca.wait_for_rows(session, collection_id, opts.collector_timeout)

    rows: List[Row] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("status") == "dead_page":
            continue
        car_id = car_id_from_url(str(item.get("product_page_url") or ""))
        if not car_id:
            continue
        # Rows come back in render order, which is page order, so the page is
        # recoverable from the position.
        row = empty_row(car_id, index // SITE_PAGE_SIZE + 1)
        apply_detail(row, {"vehicle_no": item.get("car_number")})
        if item.get("model_info"):
            row["model_info"] = item["model_info"]
        rows.append(row)

    if pages is not None:
        wanted = set(pages)
        rows = [r for r in rows if r["page"] in wanted]

    # The collector already paid for every detail page; stored details only
    # fill in what its two fields leave empty.
    split_known(rows, known)
    rows = dedupe(rows)
    coverage = {
        "everyPage": False,
        "partial": pages is None,
        "pagesRequested": max_page if pages is None else len(pages),
        "collectionId": collection_id,
    }
    return _result("collector", "brightdata-collector", rows, coverage)


# Fastest first. The order is the ladder.
ROUTES: Dict[str, Callable[..., Crawl]] = {
    "api-direct": api_route("direct"),
    "api-scrapingbee": api_route("scrapingbee"),
    "api-brightdata": api_route("brightdata"),
    "page-local": page_local,
    "collector": collector,
}


def unavailable(name: str) -> Optional[str]:
    """Why a route cannot run here, or None. A missing credential is a skip,
    not a failure."""
    from .collector import api_token
    from .transport import brightdata_available, scrapingbee_available

    if name == "api-scrapingbee" and not scrapingbee_available():
        return "no SCRAPINGBEE_KEY in .env"
    if name == "api-brightdata" and not brightdata_available():
        return "no Bright Data credentials in .env"
    if name == "collector" and not api_token():
        return "no API_TOKEN_BRINGDATA in .env"
    return None
