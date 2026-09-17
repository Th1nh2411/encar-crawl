#!/usr/bin/env python3
"""
export_listings.py -- Encar listings by page, car number + model info only.

    python listing_export/export_listings.py                  # 일반등록, page 1, 20 cars
    python listing_export/export_listings.py --pages 3
    python listing_export/export_listings.py --pages 2-5 --csv out.csv
    python listing_export/export_listings.py --pages all --out-dir
    python listing_export/export_listings.py --section premium --source api

What it does
------------
One listing request per page, then one detail request per car. The detail call
is unavoidable: the listing endpoint does not carry the vehicle number, and
that is the field being asked for.

    GET api.encar.com/search/car/list/general?q=<model>&sr=|ModifiedDate|<off>|20
    GET api.encar.com/v1/readside/vehicle/<id>?include=CATEGORY   (per car)

`--section general` is the 일반등록 tab and is the default; `premium` is the
paid-placement tab. Both cover the same cars and differ only in ordering.

Paging is 1-based, the way the site numbers it, and maps to Encar's offset.
`--pages all` reads Count from the first page and walks the rest.

Two sources, one result shape
-----------------------------
`--source page` (the default) renders the real search page in a local browser
and reads its own XHR, then takes the car number from the detail page's HTML;
`--source api` calls api.encar.com directly. Either way a run is one document:
metadata, then one row per car (page, car_id, car_number, model_info). See
results.py -- the same shape brightdata_collector and stock_pipeline write.

Where the speed comes from (--source api)
-----------------------------------------
- `include=CATEGORY` on the detail call. The full payload is ~11 KB; with this
  it is ~0.9 KB and still carries vehicleNo and the whole category block. That
  is 12x less to transfer and parse, on the 20 requests that dominate the run.
- The detail calls go out as one batch on a small thread pool over a pooled,
  cookie-less Session, so the TLS handshakes happen once and get reused.
- No cookie warm-up. The API answers an anonymous request with no headers at
  all, so opening encar.com first buys nothing here.

Bright Data is the fallback, not the default: see http_transport.py. With
`--transport auto` it is reached only after Encar throttles the run.

Nothing is written unless asked for: the result goes to stdout as JSON, and to
files with --json / --csv / --out-dir.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)
load_dotenv(os.path.join(ROOT_DIR, ".env"))

sys.path.insert(0, PKG_DIR)
import results  # noqa: E402
from http_transport import (  # noqa: E402
    Req, Result, brightdata_available, make_transport,
)

TOOL = "listing_export"
DEFAULT_OUT_DIR = os.path.join(ROOT_DIR, "output", TOOL)

# The two listing sections the site shows as tabs. 일반등록 is `general`;
# `premium` is the paid-placement tab. Both cover the same set -- the Count is
# identical -- and differ in ordering, so the section is a view, not a filter.
SEARCH_URL = "https://api.encar.com/search/car/list/{section}"
SECTIONS = ("general", "premium")
DEFAULT_SECTION = "general"

DETAIL_URL = "https://api.encar.com/v1/readside/vehicle/{car_id}"
MODELS_FILE = os.path.join(ROOT_DIR, "models.json")

DEFAULT_MODEL = "bmw_3series_g20"
DEFAULT_LIMIT = 20
DEFAULT_WORKERS = 8

# Detail calls are issued in chunks so a long run reports progress and has a
# place to pace itself. One page's worth of cars is far below this.
DETAIL_CHUNK = 100

# Listing pages are chunked for the same reason. A 1130-car model is 57 pages
# and fits in one batch; a 16000-car one is 800, and sending those with nothing
# between them is how a run spends its whole rate-limit budget before it has
# fetched a single car.
LISTING_CHUNK = 50

# A listing page that survives the in-batch retries gets one more attempt after
# this, which is longer than the 10s those use: what is left at that point is a
# block that needs real time, not another immediate retry.
LISTING_RETRY_BACKOFF = 30.0

# What Encar's rate limit looks like from here, measured over a session of runs
# rather than read off any documentation:
#
#   - 1187 requests in 33s went through, ~20 of them throttled mid-run.
#   - The next run, with no pause, was refused at its first request.
#   - After 3 minutes still refused; after 8, fine.
#   - Dropping workers from 8 to 2 changed nothing, so it counts requests, not
#     how many are in flight.
#
# It behaves like a budget of ~1200 requests over a window of a few minutes. A
# run under that needs no brake; a run over it has to hold a sustainable rate
# instead, which is that budget spread across the window.
RATE_BUDGET = 1200
RATE_WINDOW = 300.0
SAFE_RATE = RATE_BUDGET / RATE_WINDOW  # 4 requests/second

# How long a chunk of requests takes on its own, measured at ~36 req/s over 8
# workers. The pause is whatever is left of the time the chunk is allowed.
CHUNK_SECONDS = 2.8


def auto_delay(requests: int, chunk: int) -> float:
    """Seconds to pause between chunks for a run of `requests` requests.

    Zero for anything that fits inside the budget -- a small run should not be
    slowed down to guard against a limit it will never reach.
    """
    if requests <= RATE_BUDGET:
        return 0.0
    return max(round(chunk / SAFE_RATE - CHUNK_SECONDS, 1), 0.0)


def paced(delay: Optional[float], requests: int, chunk: int, what: str) -> float:
    """Resolve `delay`, where None means "work it out from the size"."""
    if delay is not None:
        return delay
    value = auto_delay(requests, chunk)
    if value:
        log(f"    pacing {what}: {requests} requests > {RATE_BUDGET} budget "
            f"-> {value:.0f}s between chunks")
    return value

# Encar's rate limit needs real time to clear from this host, so a retry a
# second later just burns the attempt. Escalating to Bright Data is both faster
# and likelier to work; the wait is what is left when there is nothing to
# escalate to.
THROTTLE_BACKOFF: float = 20.0

# How many times a throttled request is repeated. main.py lowers this, because
# it has a slower route to fall through to and needs the block established
# rather than waited out.
MAX_THROTTLE_ROUNDS: int = 3

# Retries for the very first request of a run, which is the listing call that
# every other request waits on. Throttled there means the host is blocked, not
# that this one call was unlucky: measured, a retry 10s later comes back 407
# again, while mid-run throttles clear on the first retry. None follows
# MAX_THROTTLE_ROUNDS; main.py sets 0 because it has another route to try and
# the 10s buys nothing.
FIRST_BATCH_ROUNDS: Optional[int] = None

# On Bright Data the picture is different. CloudFront throttles its exit IPs
# individually and unpredictably -- measured over repeated runs, the search
# endpoint came back 407 on roughly two attempts in three. Each batch opens a
# fresh CDP connection and so tends to land on a different exit IP, which makes
# retrying there cheap and worthwhile where waiting is not.
BD_RETRY_PAUSE = 2.0


def log(msg: str) -> None:
    """Progress goes to stderr so stdout stays a clean JSON document."""
    print(msg, file=sys.stderr, flush=True)


def load_model_spec(key: str) -> Dict[str, str]:
    """The model entry from the shared models.json in the project root."""
    try:
        with open(MODELS_FILE, encoding="utf-8") as fh:
            specs = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"models.json not found at {MODELS_FILE}")

    for spec in specs:
        if spec.get("key") == key:
            return spec

    known = ", ".join(s.get("key", "?") for s in specs)
    raise SystemExit(f"Unknown model {key!r}. models.json has: {known}")


def model_query(spec: Dict[str, str]) -> str:
    """Encar's search expression for a spec, at whatever depth it gives.

    `Hidden.N` restricts it to listings currently advertised. `car_type` is the
    domestic flag -- Y for Korean makes, N for imports -- and defaults to N,
    matching the existing entries. The client does the URL-encoding.

    The expression nests one level per field, and stops at the last one the
    spec actually fills in, so the same models.json entry shape covers a single
    model or a whole manufacturer:

        model      (And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._.
                   (C.ModelGroup.3시리즈._.Model.3시리즈 (G20_).))))
        group      (And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._.
                   ModelGroup.3시리즈.)))
        make       (And.Hidden.N._.(C.CarType.N._.Manufacturer.벤츠.))

    Note the shape change at the innermost level: a field with a child below it
    is written `(C.Field.value._.<child>)`, and the last one is just
    `Field.value.` -- which is why this is built rather than formatted.
    """
    levels = [("CarType", spec.get("car_type") or "N")]
    for field, key in (("Manufacturer", "manufacturer"),
                       ("ModelGroup", "model_group"),
                       ("Model", "model")):
        value = spec.get(key)
        if not value:
            break  # a gap ends the chain: no Model without its ModelGroup
        levels.append((field, value))

    def nest(rest: List[Tuple[str, str]]) -> str:
        field, value = rest[0]
        if len(rest) == 1:
            return f"{field}.{value}."
        return f"(C.{field}.{value}._.{nest(rest[1:])})"

    return f"(And.Hidden.N._.{nest(levels)})"


class FailoverFetcher:
    """Runs batches, and moves the whole run to Bright Data when throttled.

    Escalation is one-way. Once Encar has rate limited this host, going back to
    direct would only walk into the same limit, so the remaining batches stay
    on the fallback for the rest of the run.
    """

    def __init__(self, mode: str, workers: int):
        self.mode = mode
        self.workers = workers
        self.escalated = mode in ("brightdata", "scrapingbee")
        self.transport = make_transport(
            mode if mode in ("brightdata", "scrapingbee") else "direct", workers
        )

    def _escalate(self) -> bool:
        if self.escalated or self.mode != "auto" or not brightdata_available():
            return False
        self.transport.close()
        self.transport = make_transport("brightdata", self.workers)
        self.escalated = True
        return True

    def run(self, reqs: Sequence[Req],
            rounds: Optional[int] = None) -> List[Result]:
        """One batch, re-running whatever Encar throttled until it lands.

        `rounds` overrides how many times a throttled request is repeated.
        Waiting is worth it mid-run, where a throttle is a passing dip and the
        alternative is losing those cars; it is not worth it on the very first
        request, where a throttle means the host is blocked outright and a
        caller with another route to try is better off hearing that now. See
        FIRST_BATCH_ROUNDS.
        """
        batch = self.transport.get_many(reqs)

        for _ in range(MAX_THROTTLE_ROUNDS if rounds is None else rounds):
            throttled = [i for i, r in enumerate(batch) if r.throttled]
            if not throttled:
                break

            if self._escalate():
                log(f"    throttled on {len(throttled)} request(s) "
                    f"-> switching to {self.transport.name}")
            elif self.escalated:
                # The connection is held across batches for speed, so getting a
                # different exit IP means asking for a new one explicitly.
                reconnect = getattr(self.transport, "reconnect", None)
                if reconnect is not None:
                    reconnect()
                log(f"    {self.transport.name}: {len(throttled)} request(s) got "
                    f"407, retrying on a fresh exit IP")
                time.sleep(BD_RETRY_PAUSE)
            else:
                log(f"    throttled on {len(throttled)} request(s), "
                    f"waiting {THROTTLE_BACKOFF:.0f}s")
                time.sleep(THROTTLE_BACKOFF)

            # Only the throttled ones are worth repeating; the rest landed.
            retry = self.transport.get_many([reqs[i] for i in throttled])
            for i, result in zip(throttled, retry):
                batch[i] = result

        return batch

    @property
    def name(self) -> str:
        return self.transport.name + (" (after throttle)" if self.escalated
                                      and self.mode == "auto" else "")

    def close(self) -> None:
        self.transport.close()


def detail_model_info(category: Dict[str, Any], fallback: str) -> str:
    """Readable model string, from the most specific parts available.

    The detail endpoint's category block carries the grade and grade detail the
    listing summary does not, which is the reason this is worth a request.
    """
    parts = [
        category.get("manufacturerName"),
        category.get("modelName"),
        category.get("gradeName"),
        category.get("gradeDetailName"),
    ]
    joined = " ".join(str(p) for p in parts if p)
    return joined or fallback


def listing_model_info(item: Dict[str, Any]) -> str:
    return " ".join(
        str(p) for p in (item.get("Manufacturer"), item.get("Model"), item.get("Badge"))
        if p
    )


def parse_pages(text: str) -> Optional[List[int]]:
    """`--pages` accepts "3", "2-5" or "all". None means every page."""
    text = text.strip().lower()
    if text == "all":
        return None

    if "-" in text:
        first, _, last = text.partition("-")
        try:
            lo, hi = int(first), int(last)
        except ValueError:
            raise SystemExit(f"Bad --pages value: {text!r}")
        if lo < 1 or hi < lo:
            raise SystemExit(f"Bad page range: {text!r}")
        return list(range(lo, hi + 1))

    try:
        page = int(text)
    except ValueError:
        raise SystemExit(f"Bad --pages value: {text!r} (try 1, 2-5 or all)")
    if page < 1:
        raise SystemExit("Pages are 1-based.")
    return [page]


def listing_request(section: str, query: str, page: int, limit: int,
                    want_count: bool) -> Req:
    """Encar pages by offset; `page` is 1-based the way the site numbers it."""
    return (SEARCH_URL.format(section=section), {
        "count": "true" if want_count else "false",
        "q": query,
        "sr": f"|ModifiedDate|{(page - 1) * limit}|{limit}",
    })


def row(page: Any, car_id: Any, car_number: Optional[str],
        model_info: str) -> Dict[str, Any]:
    """The one row shape this tool emits -- see results.ROW_FIELDS."""
    return {
        "page": page,
        "car_id": str(car_id),
        "car_number": car_number,
        "model_info": model_info,
    }


def throttle_hint(fetcher: "FailoverFetcher", result: Result) -> str:
    if result.throttled and fetcher.escalated:
        return (f"\nCloudFront threw 407 at every Bright Data exit IP this run "
                f"tried ({MAX_THROTTLE_ROUNDS} of them). That is normal for the "
                f"search endpoint from datacenter IPs -- run it again, or use "
                f"--transport direct.")
    if result.throttled:
        return ("\nEncar is rate limiting this host. The block accumulates and "
                "can take several minutes to clear -- wait, lower --workers, or "
                "use --transport brightdata.")
    return ""


def fetch_listing_pages(fetcher: "FailoverFetcher", section: str, query: str,
                        pages: Optional[List[int]], limit: int,
                        coverage: Optional[Dict[str, Any]] = None,
                        delay: Optional[float] = 0.0,
                        cache=None) -> List[Dict[str, Any]]:
    """Listing items for the requested pages, in page order.

    The first page is fetched on its own because it is what reports `Count`,
    and with `--pages all` that Count is what says how many pages there are.
    The rest go out as one batch: they are independent requests and there is
    nothing to gain by walking them one at a time.

    A page that never lands is the dangerous case: its 20 cars are simply
    absent from the result, and every later count -- "1020/1020 car numbers" --
    is computed over what did arrive, so a run that lost 110 cars still reports
    itself as complete. `coverage` is filled in with what was asked for against
    what arrived, so the caller can tell the difference.
    """
    first_page = 1 if pages is None else pages[0]
    first = fetcher.run([listing_request(section, query, first_page, limit, True)],
                        rounds=FIRST_BATCH_ROUNDS)[0]
    if not first.ok:
        raise SystemExit(f"Could not read page {first_page}: {first.error}"
                         f"{throttle_hint(fetcher, first)}")

    total = int((first.data or {}).get("Count") or 0)
    total_pages = max(1, math.ceil(total / limit)) if total else 1

    if pages is None:
        pages = list(range(1, total_pages + 1))
        log(f"  {total} listings -> {total_pages} pages of {limit}")
    else:
        wanted = [p for p in pages if p <= total_pages]
        if len(wanted) != len(pages):
            log(f"  note: only {total_pages} pages exist ({total} listings)")
        pages = wanted or [first_page]

    items = list((first.data or {}).get("SearchResults") or [])
    for it in items:
        it["_page"] = first_page

    def fetch_pages(page_nos: List[int], pause: float) -> List[int]:
        """Fetch these pages into `items`; return the ones that never landed.

        Chunked, because one batch of 800 listing requests is 800 requests
        with nothing between them -- which is exactly how a run walks into the
        rate limit. `--delay` applies here for the same reason it applies to
        the detail calls: it is the only brake the run has.
        """
        missed: List[int] = []
        for start in range(0, len(page_nos), LISTING_CHUNK):
            chunk = page_nos[start:start + LISTING_CHUNK]
            batch = fetcher.run([
                listing_request(section, query, p, limit, False) for p in chunk
            ])
            for page_no, result in zip(chunk, batch):
                if not result.ok:
                    log(f"    ! page {page_no}: {result.error}")
                    missed.append(page_no)
                    continue
                page_items = (result.data or {}).get("SearchResults") or []
                for it in page_items:
                    it["_page"] = page_no
                items.extend(page_items)

            if len(page_nos) > LISTING_CHUNK:
                done = min(start + LISTING_CHUNK, len(page_nos))
                log(f"    listing {done}/{len(page_nos)} pages")
                if pause and done < len(page_nos):
                    time.sleep(pause)
        return missed

    rest = [p for p in pages if p != first_page]
    # Now that Count is in, the size of the run is known: every page costs one
    # listing request and up to `limit` detail ones -- minus whatever the cache
    # already covers, which on a repeat run is most of them. Counting those as
    # requests would brake a run that is not going to make them.
    cached = len(getattr(cache, "entries", ()) or ()) if cache is not None else 0
    estimate = len(pages) * (1 + limit) - min(cached, len(pages) * limit)
    pace = paced(delay, estimate, LISTING_CHUNK, "listing")
    failed = fetch_pages(rest, pace) if rest else []

    # A listing page that never landed takes its whole page of cars with it, so
    # it is worth one more try after a longer wait than the in-batch retries
    # use. Measured: a throttle that survives three 10s rounds has usually
    # cleared by the time this comes round.
    if failed:
        log(f"  {len(failed)} listing page(s) missing, retrying after "
            f"{LISTING_RETRY_BACKOFF:.0f}s")
        time.sleep(LISTING_RETRY_BACKOFF)
        failed = fetch_pages(sorted(failed), pace)

    if coverage is not None:
        # The last page holds the remainder, so the expected count is the
        # listing total clipped to the pages actually asked for.
        expected = sum(limit if p < total_pages else total - (total_pages - 1) * limit
                       for p in pages) if total else len(items)
        coverage.update({
            "listingTotal": total,
            "pagesRequested": len(pages),
            "pagesFailed": sorted(failed),
            "expectedCars": max(expected, 0),
            "missingCars": max(expected - len(items), 0),
        })

    if failed:
        log(f"  ! {len(failed)} of {len(pages)} listing page(s) never landed: "
            f"about {len(failed) * limit} cars are missing from this run")

    log(f"  listing: {len(items)} cars from {len(pages)} page(s) "
        f"of {total} total")
    return items


def fetch_car_numbers(fetcher: "FailoverFetcher", items: List[Dict[str, Any]],
                      delay: Optional[float], cache=None) -> List[Dict[str, Any]]:
    """Car number and model info per listing, in chunks so progress is visible.

    With a cache, the ids it already knows never become requests at all: a
    vehicle number does not change, so the only cars worth asking about are the
    ones this host has not seen before.
    """
    rows: List[Dict[str, Any]] = []
    # Cached cars are not fetched, so they would otherwise all end up ahead of
    # the fetched ones. The listing order is the order the site gave, and the
    # output is expected to be in it.
    order = {str(it.get("Id")): i for i, it in enumerate(items)}

    if cache is not None and cache.enabled:
        known, items = cache.split(items)
        for item, entry in known:
            rows.append(row(item.get("_page"), item.get("Id"),
                            entry.get("car_number"),
                            entry.get("model_info") or listing_model_info(item)))
        if known:
            log(f"  cache: {len(known)} known, {len(items)} to fetch")

    # Cache hits are not requests, so the pace is set by what is left to fetch.
    delay = paced(delay, len(items), DETAIL_CHUNK, "detail")

    for start in range(0, len(items), DETAIL_CHUNK):
        chunk = items[start:start + DETAIL_CHUNK]
        batch = fetcher.run([
            (DETAIL_URL.format(car_id=it.get("Id")), {"include": "CATEGORY"})
            for it in chunk
        ])

        for item, detail in zip(chunk, batch):
            fallback = listing_model_info(item)
            data = detail.data or {}
            # A failed detail call leaves car_number None rather than dropping
            # the row, so the output always matches the pages that were asked
            # for -- a missing number is visible instead of silently absent.
            if not detail.ok and not detail.gone:
                log(f"    ! detail {item.get('Id')}: {detail.error}")
            car_number = data.get("vehicleNo") or None
            info = (detail_model_info(data.get("category") or {}, fallback)
                    if detail.ok else fallback)
            rows.append(row(item.get("_page"), item.get("Id"), car_number, info))
            if cache is not None:
                cache.remember(item.get("Id"), car_number, info)

        if len(items) > DETAIL_CHUNK:
            done = min(start + DETAIL_CHUNK, len(items))
            log(f"    detail {done}/{len(items)}")
            if delay and done < len(items):
                time.sleep(delay)

    rows.sort(key=lambda r: order.get(str(r["car_id"]), len(order)))
    return rows


def run_summary(model_key: str, spec: Dict[str, str], section: str, source: str,
                transport: str, rows: List[Dict[str, Any]], elapsed: float,
                coverage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The metadata half of the output document; results.save() adds the rows.

    `complete` is the field to read before trusting a run: a listing page that
    never landed takes its 20 cars with it, and every other count here is
    computed over the rows that did arrive.
    """
    summary = {
        "model": model_key,
        "label": spec.get("label"),
        "section": section,
        "source": source,
        "transport": transport,
        "pages": sorted({r["page"] for r in rows}),
        "carNumbersFilled": sum(1 for r in rows if r["car_number"]),
        "elapsedSeconds": round(elapsed, 2),
    }
    if coverage:
        summary.update(coverage)
        summary["complete"] = not coverage.get("missingCars")
    return summary


def export_via_browser(model_key: str, section: str, pages: Optional[List[int]],
                       limit: int, workers: int, with_detail: bool = True,
                       delay: Optional[float] = 0.0, browser: str = "local",
                       cache=None) -> Dict[str, Any]:
    """`--source page`: read the site the way a browser does. See browser_source.py.

    The listing comes from the search page's own XHR and the car number from
    the detail page's HTML, so no Encar API is called directly. Both steps go
    out from this host: routing the detail pages through the Bright Data proxy
    zone was tried and removed, because a zone without KYC enforces Encar's
    robots.txt and answers 402 on every one of them.
    """
    from browser_source import BrowserSource
    from http_transport import brightdata_ws_url

    spec = load_model_spec(model_key)
    action = model_query(spec)
    source = BrowserSource(
        ws_url=brightdata_ws_url(), workers=workers, log=log, browser=browser,
    )
    started = time.time()

    try:
        log(f"Model {model_key} ({spec.get('label', '?')}), section {section}, "
            f"source page (listing: {browser} browser, detail: direct)")

        # With `all`, page 1 is what reports Count, so the rest is only known
        # after it lands.
        queue = [1] if pages is None else list(pages)
        rows: List[Dict[str, Any]] = []
        total: Optional[int] = None
        done: set = set()

        while queue:
            page_no = queue.pop(0)
            if page_no in done:
                continue
            done.add(page_no)

            data = source.listing_page(action, page_no, limit, section)
            if data is None:
                log(f"    ! page {page_no}: blocked, no data")
                continue

            if total is None:
                total = int(data.get("Count") or 0)
                if pages is None:
                    total_pages = max(1, math.ceil(total / limit)) if total else 1
                    log(f"  {total} listings -> {total_pages} pages of {limit}")
                    queue.extend(range(2, total_pages + 1))

            items = data.get("SearchResults") or []
            log(f"  page {page_no}: {len(items)} cars")

            if not with_detail:
                found = [(None, listing_model_info(it)) for it in items]
            elif cache is not None and cache.enabled:
                # Detail pages are this route's slow half -- one HTTP fetch of
                # ~200 KB per car -- so a cache hit saves far more here than on
                # the API route.
                known, to_fetch = cache.split(items)
                found_map = {str(it.get("Id")): (e.get("car_number"),
                                                 e.get("model_info") or
                                                 listing_model_info(it))
                             for it, e in known}
                if known:
                    log(f"    cache: {len(known)} known, {len(to_fetch)} to fetch")
                for it, (car_number, info) in zip(to_fetch,
                                                  source.car_numbers(to_fetch)):
                    found_map[str(it.get("Id"))] = (car_number, info)
                    cache.remember(it.get("Id"), car_number, info)
                found = [found_map.get(str(it.get("Id")), (None, listing_model_info(it)))
                         for it in items]
            else:
                found = source.car_numbers(items)

            for item, (car_number, info) in zip(items, found):
                rows.append(row(page_no, item.get("Id"), car_number, info))

            if delay and queue:
                time.sleep(delay)

        elapsed = time.time() - started
        filled = sum(1 for r in rows if r["car_number"])
        log(f"  {filled}/{len(rows)} car numbers in {elapsed:.2f}s (page source)")

        summary = run_summary(model_key, spec, section, "page",
                              f"{browser}-browser", rows, elapsed)
        if cache is not None:
            summary.update(cache.summary())
        return {"rows": rows, **summary}
    finally:
        source.close()


def export_via_api(model_key: str, section: str, pages: Optional[List[int]],
                   limit: int, workers: int, mode: str, with_detail: bool = True,
                   delay: Optional[float] = 0.0, cache=None) -> Dict[str, Any]:
    """`--source api`: call api.encar.com directly, with Bright Data as fallback."""
    spec = load_model_spec(model_key)
    fetcher = FailoverFetcher(mode, workers)
    started = time.time()

    try:
        log(f"Model {model_key} ({spec.get('label', '?')}), "
            f"section {section}, transport {fetcher.name}")

        coverage: Dict[str, Any] = {}
        items = fetch_listing_pages(fetcher, section, model_query(spec), pages,
                                    limit, coverage, delay, cache)

        if with_detail:
            rows = fetch_car_numbers(fetcher, items, delay, cache)
        else:
            # Without the detail pass there is no car number to be had; the
            # listing carries the model only.
            rows = [row(it.get("_page"), it.get("Id"), None, listing_model_info(it))
                    for it in items]

        elapsed = time.time() - started
        filled = sum(1 for r in rows if r["car_number"])
        missing = coverage.get("missingCars") or 0
        of_expected = (f" of {coverage['expectedCars']} expected"
                       if missing else "")
        log(f"  {filled}/{len(rows)}{of_expected} car numbers in "
            f"{elapsed:.2f}s ({fetcher.name})")

        summary = run_summary(model_key, spec, section, "api", fetcher.name,
                              rows, elapsed, coverage)
        if cache is not None:
            summary.update(cache.summary())
        return {"rows": rows, **summary}
    finally:
        fetcher.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Encar listings by page: car number + model info.")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"models.json key (default: {DEFAULT_MODEL})")
    parser.add_argument("--section", choices=SECTIONS, default=DEFAULT_SECTION,
                        help=f"listing tab: general = 일반등록 "
                             f"(default: {DEFAULT_SECTION})")
    parser.add_argument("--pages", default="1",
                        help="page number, range or all -- e.g. 1, 2-5, all "
                             "(default: 1)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"cars per page (default: {DEFAULT_LIMIT})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"requests in flight (default: {DEFAULT_WORKERS})")
    parser.add_argument("--delay", type=float, default=None,
                        help=f"seconds to pause between chunks of requests. "
                             f"Left out, it is worked out from the size of the "
                             f"run once Count is known: nothing below Encar's "
                             f"budget of ~{RATE_BUDGET} requests, and enough "
                             f"to hold ~{SAFE_RATE:.0f} requests/second above "
                             f"it. 0 disables it")
    parser.add_argument("--no-detail", action="store_true",
                        help="skip the per-car requests -- fast, but no car number")
    parser.add_argument("--source", choices=("page", "api"), default="page",
                        help="page (default) renders the real search page in the "
                             "browser and reads its own XHR, then takes the car "
                             "number from the detail page HTML; api calls "
                             "api.encar.com directly")
    parser.add_argument("--browser", choices=("local", "brightdata"),
                        default="local",
                        help="--source page only: which browser renders the "
                             "listing. local is the default because Bright Data "
                             "cannot load the site's JavaScript (see README)")
    parser.add_argument("--transport", choices=("auto", "direct", "brightdata"),
                        default="brightdata",
                        help="--source api only: brightdata (default) routes every "
                             "request through Bright Data; direct goes straight "
                             "out; auto goes direct and fails over when throttled")
    parser.add_argument("--json", dest="json_out", help="write the run to this JSON file")
    parser.add_argument("--csv", dest="csv_out", help="write the rows to this CSV file")
    parser.add_argument("--out-dir", nargs="?", const=DEFAULT_OUT_DIR,
                        help=f"write both, named <model>_<timestamp> in this "
                             f"directory (default: "
                             f"{os.path.relpath(DEFAULT_OUT_DIR, ROOT_DIR)})")
    parser.add_argument("--quiet", action="store_true",
                        help="do not print the JSON document to stdout")
    args = parser.parse_args()

    pages = parse_pages(args.pages)

    if args.source == "api" and args.transport != "direct" and not brightdata_available():
        log("  note: no Bright Data credentials in .env, direct only")

    try:
        if args.source == "page":
            run = export_via_browser(args.model, args.section, pages, args.limit,
                                     args.workers, with_detail=not args.no_detail,
                                     delay=args.delay, browser=args.browser)
        else:
            run = export_via_api(args.model, args.section, pages, args.limit,
                                 args.workers, args.transport,
                                 with_detail=not args.no_detail, delay=args.delay)
    except RuntimeError as exc:  # transport misconfiguration
        raise SystemExit(str(exc))

    rows = run.pop("rows")
    for path in results.save(TOOL, args.model, rows, run, json_path=args.json_out,
                             csv_path=args.csv_out, out_dir=args.out_dir):
        log(f"  wrote {path}")

    if not args.quiet:
        json.dump(results.envelope(TOOL, args.model, rows, run), sys.stdout,
                  ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
