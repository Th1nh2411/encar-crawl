"""
api.py -- Reading Encar's JSON API: the listing pages, then the per-car detail.

    GET api.encar.com/search/car/list/general?q=<model>&sr=|ModifiedDate|<off>|<n>
    GET api.encar.com/v1/readside/vehicle/<id>?include=CATEGORY,SPEC   (per car)

The detail call is unavoidable for a car seen for the first time: the listing
endpoint does not carry the vehicle number. Once stored it is never asked for
again (see parse.split_known), which is what keeps a repeat crawl far under
Encar's rate limit.

The transport underneath -- direct, ScrapingBee, Bright Data -- is chosen by
the route (routes.py); everything here is the same whichever one it is.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import SITE_PAGE_SIZE
from .log import log
from .parse import Row, apply_detail, detail_fields, listing_row
from .transport import Req, Result, make_transport

SEARCH_URL = "https://api.encar.com/search/car/list/{section}"
DETAIL_URL = "https://api.encar.com/v1/readside/vehicle/{car_id}"
DETAIL_INCLUDE = "CATEGORY,SPEC"

# Requests are issued in chunks so a long run reports progress and has a place
# to pace itself. A 16000-car model is 80 listing pages at 200 a page; sending
# a few hundred requests with nothing between them is how a run spends its
# whole rate-limit budget before it has fetched a single car.
LISTING_CHUNK = 50
DETAIL_CHUNK = 100

# A listing page that survives the in-batch retries gets one more attempt
# after this: what is left at that point is a block that needs real time.
LISTING_RETRY_BACKOFF = 30.0

# What Encar's rate limit looks like from here, measured over a session of
# runs rather than read off any documentation:
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

# Two situations that both look like "throttled":
#
#   first request   the host is blocked outright. Measured: a retry 10s later
#                   comes back 407 again. The ladder has another route to fall
#                   through to, so waiting is pure loss -- fail at once.
#   mid-run         a passing dip. Measured on a 1130-car run: 25 detail calls
#                   were throttled and the retry 10s later got all 25. Waiting
#                   here buys real cars, so it stays.
FIRST_BATCH_ROUNDS = 0
MAX_THROTTLE_ROUNDS = 3
THROTTLE_BACKOFF = 10.0

# Through a proxy service a throttle is one exit IP, not this host. A fresh
# connection tends to land on another, so retrying there is cheap and worth it.
PROXY_RETRY_PAUSE = 2.0


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


class Fetcher:
    """One transport, and the retrying of whatever Encar throttled."""

    def __init__(self, kind: str, workers: int):
        self.transport = make_transport(kind, workers)

    @property
    def name(self) -> str:
        return self.transport.name

    def run(self, reqs: Sequence[Req], rounds: Optional[int] = None) -> List[Result]:
        """One batch, re-running the throttled requests until they land."""
        batch = self.transport.get_many(reqs)

        for _ in range(MAX_THROTTLE_ROUNDS if rounds is None else rounds):
            throttled = [i for i, r in enumerate(batch) if r.throttled]
            if not throttled:
                break

            reconnect = getattr(self.transport, "reconnect", None)
            if reconnect is not None:
                # The connection is held across batches for speed, so getting
                # a different exit IP means asking for a new one explicitly.
                reconnect()
                log(f"    {self.name}: {len(throttled)} request(s) throttled, "
                    f"retrying on a fresh exit IP")
                time.sleep(PROXY_RETRY_PAUSE)
            else:
                log(f"    throttled on {len(throttled)} request(s), "
                    f"waiting {THROTTLE_BACKOFF:.0f}s")
                time.sleep(THROTTLE_BACKOFF)

            # Only the throttled ones are worth repeating; the rest landed.
            retry = self.transport.get_many([reqs[i] for i in throttled])
            for i, result in zip(throttled, retry):
                batch[i] = result

        return batch

    def close(self) -> None:
        self.transport.close()


def listing_request(section: str, query: str, page: int, limit: int,
                    want_count: bool) -> Req:
    """Encar pages by offset; `page` is 1-based the way the site numbers it."""
    return (SEARCH_URL.format(section=section), {
        "count": "true" if want_count else "false",
        "q": query,
        "sr": f"|ModifiedDate|{(page - 1) * limit}|{limit}",
    })


def expected_cars(total: int, pages: List[int], limit: int) -> int:
    """Cars the requested pages should hold; the last page holds the remainder."""
    if not total:
        return 0
    total_pages = max(1, math.ceil(total / limit))
    return sum(limit if p < total_pages else total - (total_pages - 1) * limit
               for p in pages if p <= total_pages)


def fetch_listing(fetcher: Fetcher, section: str, query: str,
                  pages: Optional[List[int]], limit: int, delay: Optional[float],
                  known: int = 0) -> Tuple[List[Row], Dict[str, Any]]:
    """Listing rows for the requested pages, plus what was asked against what came.

    The first page is fetched on its own because it reports `Count`, and with
    every page asked for, Count is what says how many there are.

    A page that never lands is the dangerous case: its cars are simply absent,
    and every later count is computed over what did arrive -- so a run that
    lost 110 cars still looks complete. The coverage returned here is what lets
    the caller tell the difference, and what sold detection is gated on.
    """
    first_page = 1 if pages is None else pages[0]
    first = fetcher.run([listing_request(section, query, first_page, limit, True)],
                        rounds=FIRST_BATCH_ROUNDS)[0]
    if not first.ok:
        raise RuntimeError(f"Could not read page {first_page}: {first.error}")

    total = int((first.data or {}).get("Count") or 0)
    total_pages = max(1, math.ceil(total / limit)) if total else 1
    every_page = pages is None

    if pages is None:
        pages = list(range(1, total_pages + 1))
        log(f"  {total} listings -> {total_pages} page(s) of {limit}")
    else:
        wanted = [p for p in pages if p <= total_pages]
        if len(wanted) != len(pages):
            log(f"  note: only {total_pages} page(s) exist ({total} listings)")
        pages = wanted or [first_page]

    def page_rows(page_no: int, items: List[Dict[str, Any]]) -> List[Row]:
        # Rows are numbered by the site's page of 20, whatever size was
        # requested, so a row can be found again on the website.
        base = (page_no - 1) * limit
        return [listing_row(it, (base + i) // SITE_PAGE_SIZE + 1)
                for i, it in enumerate(items)]

    rows: List[Row] = page_rows(first_page,
                                (first.data or {}).get("SearchResults") or [])

    def fetch_pages(page_nos: List[int], pause: float) -> List[int]:
        """Fetch these pages into `rows`; return the ones that never landed."""
        missed: List[int] = []
        for start in range(0, len(page_nos), LISTING_CHUNK):
            chunk = page_nos[start:start + LISTING_CHUNK]
            batch = fetcher.run([listing_request(section, query, p, limit, False)
                                 for p in chunk])
            for page_no, result in zip(chunk, batch):
                if not result.ok:
                    log(f"    ! page {page_no}: {result.error}")
                    missed.append(page_no)
                    continue
                rows.extend(page_rows(page_no,
                                      (result.data or {}).get("SearchResults") or []))

            if len(page_nos) > LISTING_CHUNK:
                done = min(start + LISTING_CHUNK, len(page_nos))
                log(f"    listing {done}/{len(page_nos)} pages")
                if pause and done < len(page_nos):
                    time.sleep(pause)
        return missed

    # Now the size of the run is known: every page costs one listing request
    # and up to `limit` detail ones -- minus the cars already stored, which on
    # a repeat run is most of them.
    estimate = len(pages) * (1 + limit) - min(known, len(pages) * limit)
    pace = paced(delay, estimate, LISTING_CHUNK, "listing")
    rest = [p for p in pages if p != first_page]
    failed = fetch_pages(rest, pace) if rest else []

    if failed:
        log(f"  {len(failed)} listing page(s) missing, retrying after "
            f"{LISTING_RETRY_BACKOFF:.0f}s")
        time.sleep(LISTING_RETRY_BACKOFF)
        failed = fetch_pages(sorted(failed), pace)

    if failed:
        log(f"  ! {len(failed)} of {len(pages)} listing page(s) never landed: "
            f"about {len(failed) * limit} cars are missing from this run")

    rows = dedupe(rows)
    expected = expected_cars(total, pages, limit) if total else len(rows)
    coverage = {
        "listingTotal": total,
        "everyPage": every_page,
        "pagesRequested": len(pages),
        "pagesFetched": len(pages) - len(failed),
        "pagesFailed": sorted(failed),
        "expectedCars": expected,
        "missingCars": max(expected - len(rows), 0),
    }
    log(f"  listing: {len(rows)} cars from {coverage['pagesFetched']}/"
        f"{len(pages)} page(s) of {total} total")
    return rows, coverage


def dedupe(rows: List[Row]) -> List[Row]:
    """One row per car_id, first seen wins.

    The listing is sorted by ModifiedDate, so a car edited mid-crawl can move
    to an earlier page and be read twice.
    """
    seen: Dict[str, Row] = {}
    for row in rows:
        seen.setdefault(row["car_id"], row)
    return list(seen.values())


def fetch_details(fetcher: Fetcher, rows: List[Row],
                  delay: Optional[float]) -> int:
    """Fill the vehicle number and detail fields in place. Returns how many got one."""
    delay = paced(delay, len(rows), DETAIL_CHUNK, "detail")
    filled = 0

    for start in range(0, len(rows), DETAIL_CHUNK):
        chunk = rows[start:start + DETAIL_CHUNK]
        batch = fetcher.run([
            (DETAIL_URL.format(car_id=r["car_id"]), {"include": DETAIL_INCLUDE})
            for r in chunk
        ])
        for row, detail in zip(chunk, batch):
            # A failed call leaves vehicle_no empty rather than dropping the
            # row: the listing says the car is there, which is what sold
            # detection needs, and the number is asked for again next run.
            if not detail.ok:
                if not detail.gone:
                    log(f"    ! detail {row['car_id']}: {detail.error}")
                continue
            apply_detail(row, detail_fields(detail.data or {}))
            if row.get("vehicle_no"):
                filled += 1

        if len(rows) > DETAIL_CHUNK:
            done = min(start + DETAIL_CHUNK, len(rows))
            log(f"    detail {done}/{len(rows)}")
            if delay and done < len(rows):
                time.sleep(delay)

    return filled
