#!/usr/bin/env python3
"""
main.py -- One entry point that gets the listings out by the fastest route that
still works.

    python main.py                      # every page of 일반등록, written to output/
    python main.py --pages 1-5
    python main.py --pages 1 --print
    python main.py --strategies api-direct,page-local

`--pages all` is the default because it is cheap on the fast route: the whole
1130-car model came back in 33s with every vehicle number filled. The run is
written to output/encar_export/ as JSON and CSV; --print also dumps it to
stdout, which is off by default because a full run is 1130 rows.

Why a ladder instead of a flag
------------------------------
Every route in this project reaches the same two facts per car -- the vehicle
number and the model -- but they cost wildly different amounts of time and
money, and the cheap ones are the ones Encar blocks first. So the routes are
tried in order and the run stops at the first one that actually produces rows:

  1. api-direct   api.encar.com straight from this host: one listing call per
                  page, then one detail call per car for the vehicle number the
                  listing does not carry. Measured 1.1s for a page of 20, free.
                  Encar rate limits it (403/407/429) once a host asks too often.
  2. api-scrapingbee
                  the same JSON calls, through ScrapingBee's IPs. Still no
                  rendering, so it stays cheap in both time and bytes, and a
                  different exit IP is what an IP-level block actually needs.
                  Measured 11.9s for a page of 20. Its classic tier -- 1 credit
                  a request, no premium proxy -- gets through, which Bright
                  Data's datacenter IPs do not. What limits it is concurrency,
                  not request count: this plan allows about three at a time, so
                  it is steady rather than fast. See http_transport.py.
  3. api-brightdata
                  the same again, through Bright Data. It answers 402 on every
                  call until the zone has passed KYC, since without it the zone
                  enforces Encar's robots.txt, and `api.encar.com` says
                  `Disallow: /`. That 402 parks the route for a day rather than
                  for the throttle cooldown: it is a fact about the account,
                  not a passing block.
  4. page-local   render the real search page in a local Chromium and read its
                  own XHR, then the car number off the detail page HTML. Same
                  IP as api-direct, but it carries the site's cookies and
                  headers, so it survives a block aimed at bare API clients --
                  and dies with it when the block is on the IP. 12.7s a page.
  5. collector    the hosted Bright Data collector (trigger_collector.py). It
                  renders every list and detail page in a real browser: 85s for
                  one page, 231s for ten. Also the only route billed per run.
                  It is last because it is the only one that leaves this host's
                  network, so it is what an IP-level block falls through to --
                  and it is capped at --collector-max-page, since rendering a
                  whole model is a quarter of an hour against 33s on api-direct.

A route "failed" when it raised, when it returned no rows, when it filled fewer
than --min-fill of the car numbers, or when cars are missing outright because a
listing page never landed -- that last one is checked against the listing's own
Count, because it is otherwise invisible: a run that lost six pages still
reports "1020/1020 car numbers" over the rows that did arrive.

A route that falls short is not thrown away. The fullest one seen is kept, and
if nothing better lands it is used and the output is marked incomplete -- 1020
cars from a throttled api-direct beats 186 from the collector.

A route that is not configured is skipped, not failed: without SCRAPINGBEE_KEY
or the Bright Data credentials in .env, those routes are not attempted.

Output is the same document every other tool in this project writes (see
listing_export/results.py): metadata, then one row per car, plus `strategy` and
`attempts` recording which routes were tried and why they were dropped.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "listing_export"))

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

load_dotenv(os.path.join(ROOT_DIR, ".env"))

import results  # noqa: E402  (listing_export/results.py -- the shared row shape)
from export_listings import (  # noqa: E402
    DEFAULT_MODEL, DEFAULT_SECTION, DEFAULT_WORKERS, SECTIONS,
    export_via_api, export_via_browser, load_model_spec, model_query,
    parse_pages, row,
)
from http_transport import brightdata_available, scrapingbee_available  # noqa: E402
from browser_source import search_page_url  # noqa: E402
from plate_cache import PlateCache  # noqa: E402

import export_listings  # noqa: E402

# Two different situations that both look like "throttled":
#
#   first request   the host is blocked outright. Measured: a retry 10s later
#                   comes back 407 again. With another route to fall through
#                   to, waiting is pure loss -- fail in half a second instead.
#   mid-run         a passing dip. Measured on a 1130-car run: 25 detail calls
#                   were throttled and the retry 10s later got all 25. Waiting
#                   here buys real cars, so it stays.
export_listings.FIRST_BATCH_ROUNDS = 0
export_listings.MAX_THROTTLE_ROUNDS = 3
export_listings.THROTTLE_BACKOFF = 10.0

TOOL = "encar_export"
DEFAULT_OUT_DIR = os.path.join(ROOT_DIR, "output", TOOL)

# Where the last throttle is remembered between runs. Encar's block is a time
# window on this host, so a run started inside it pays 10s to be told what the
# previous run already knew -- and its requests count towards the window,
# holding the block open longer.
# Kept in var/, not output/. output/ holds the results of runs -- timestamped
# files that can be deleted whenever the disk needs it. These two are state the
# next run depends on, and the cache in particular is worth 16000 requests to
# rebuild, so it must not be swept up with the artefacts.
STATE_DIR = os.path.join(ROOT_DIR, "var")
STATE_FILE = os.path.join(STATE_DIR, "throttle_state.json")
THROTTLE_COOLDOWN = 300.0

# Car numbers already looked up, kept between runs. A vehicle number never
# changes, so the detail call for a car only has to happen once, ever. That is
# what keeps a repeated export off the rate limit: the second run asks about
# newly listed cars only, not about all 1130 again. See plate_cache.py.
DEFAULT_CACHE_FILE = os.path.join(STATE_DIR, "plate_cache.json")

# How long a cached car number is trusted. The cache has no way to notice that
# an entry is wrong -- its whole premise is that the answer cannot change -- so
# a bad one would otherwise last forever. 90 days costs close to nothing in
# practice: a listing rarely lives that long, so almost every entry is retired
# by the car being delisted long before it expires.
CACHE_MAX_AGE_DAYS = 90.0

# What a throttled failure looks like by the time it reaches this layer: the
# routes report it as text, not as a status code.
THROTTLE_MARKS = ("HTTP 403", "HTTP 407", "HTTP 429")

# Fastest first. Anything not configured is skipped rather than failed.
ALL_STRATEGIES = ("api-direct", "api-scrapingbee", "api-brightdata",
                  "page-local", "collector")
DEFAULT_STRATEGIES = ALL_STRATEGIES

# 402 is not a throttle, it is a verdict about the Bright Data account: the
# zone has no KYC, so it enforces Encar's robots.txt and refuses every path
# this needs. Retrying tomorrow is as pointless as retrying in ten seconds, so
# the route is parked for a day rather than for the throttle cooldown. Doing
# the KYC and re-running with --cooldown 0 brings it straight back.
ZONE_REFUSED_COOLDOWN = 86400.0

# Failures that are facts about the account rather than passing blocks, so the
# route is parked for a day instead of for the throttle cooldown:
#   402  Bright Data zone without KYC, refusing every Encar path
#   401  ScrapingBee out of calls for the month
REFUSED_MARK = "HTTP 402"
REFUSED_MARKS = ("HTTP 402", "limit reached")

# A route that comes back with fewer car numbers than this is treated as
# throttled rather than as an answer. 1 page of 20 with 3 numbers is a block,
# not a result.
DEFAULT_MIN_FILL = 0.8

# Missing cars are judged far more strictly than missing car numbers, because
# they are invisible: a run that lost 6 listing pages still reported
# "1020/1020 car numbers" and called itself ok. One page of 20 out of 1130 is
# under 2%, so this tolerates a single unlucky page and catches the rest. A run
# that trips it is not thrown away -- it is kept as the fallback if nothing
# better lands, and marked incomplete.
MISSING_TOLERANCE = 0.02

# What Encar's rate limit looks like from here, measured over a session's worth
# of runs rather than read off any documentation:
#
#   - 1187 requests in 33s went through, with ~20 of them throttled mid-run.
#   - The next run, with no pause, was refused at its first request.
#   - After 3 minutes it was still refused; after 8 it was fine.
#   - Dropping --workers from 8 to 2 changed nothing, so it counts requests,
#     not how many are in flight.
#
# So it behaves like a budget of roughly 1200 requests over a window of a few
# minutes. A run under that needs no brake at all; a run over it has to hold a
# sustainable rate instead, which is the budget spread across the window.
RATE_BUDGET = 1200.0
RATE_WINDOW = 300.0
SAFE_RATE = RATE_BUDGET / RATE_WINDOW  # 4 requests/second

# Roughly how long a chunk of requests takes on its own, measured at ~36 req/s
# over 8 workers. Used to work out how much of a pause to add on top.
CHUNK_SECONDS = 2.8

# How many pages the collector walks when the run asked for all of them.
# Measured: 85s for 1 page, 231s for 10 -- most of the 85s is fixed startup, and
# each further page costs around 16s of browser rendering. The 57 pages this
# model has would be some 15 minutes, against 33s on api-direct. It is a
# fallback for getting something out while blocked, not a way to export a model.
COLLECTOR_MAX_PAGE = 10

# Cars per listing request. The site shows 20 a page and listing_export keeps
# that, because being able to hold its output next to the page in a browser is
# the point of it. Here the page is not a thing anyone looks at, and the API
# serves 200 just as happily -- verified against it, as 500 also is, but 200 is
# the size stock_pipeline has been running on. It matters more than it looks:
# once the cache covers the cars, listing calls are the *whole* cost of a run,
# and 16000 cars is 80 requests this way against 800 at 20 a page -- the
# difference between scraping under the rate limit and sitting on top of it.
DEFAULT_LIMIT = 200

# The browser routes keep the site's own page size. They do not call the API --
# page-local reads the XHR the search page fires for itself, and the collector
# renders the page -- so the page size is the site's to choose. Ask for 200 and
# a site that only offers 20/50/100 quietly serves 20, while `--pages all`
# works out its page count from the number it asked for: too few pages, cars
# missing, nothing said. 20 is what the site shows, so 20 is what they use.
PAGE_ROUTE_LIMIT = 20


def log(msg: str) -> None:
    """Progress to stderr so stdout stays a clean JSON document."""
    print(msg, file=sys.stderr, flush=True)


class BackgroundRoute:
    """A route running on its own thread, to be collected later.

    Only worth doing for the collector, whose time is spent waiting on Bright
    Data rather than on this host: it can run while a cheaper route is tried,
    and the two do not compete for the IP that is already blocked.

    The thread is a daemon. If a cheaper route wins, the process exits without
    waiting for it -- the collector run itself finishes on Bright Data's side
    regardless, and is billed either way.
    """

    def __init__(self, name: str, call, log):
        import threading

        self.name = name
        self.started = time.time()
        self._value: Optional[Dict[str, Any]] = None
        self._error: Optional[str] = None
        self._log = log

        def body() -> None:
            try:
                self._value = call()
            except (SystemExit, Exception) as exc:
                self._error = f"{type(exc).__name__}: {exc}".strip()

        self._thread = threading.Thread(target=body, daemon=True)
        self._thread.start()

    def result(self):
        """Wait for it and return (run, reason) the way a direct call does."""
        self._thread.join()
        if self._value is None:
            reason = self._error or "failed"
            self._log(f"[{self.name}] failed after "
                      f"{time.time() - self.started:.1f}s: {reason}")
            return None, reason
        return self._value, None


def start_race(ladder: List[tuple], after: int, log) -> Optional[BackgroundRoute]:
    """Start the collector in the background if it is still ahead in `ladder`."""
    for name, skip, call in ladder[after + 1:]:
        if name != "collector" or skip:
            continue
        log(f"[{name}] starting in parallel (--race) -- "
            f"it waits on Bright Data, not on this host")
        return BackgroundRoute(name, call, log)
    return None


def looks_throttled(reason: str) -> bool:
    return any(mark in reason for mark in THROTTLE_MARKS)


def worth_racing(reason: str) -> bool:
    """Is this the kind of failure a parallel collector run would help with?

    Racing costs a billed collector run, so it is only worth starting when the
    route failed for a reason the collector is immune to -- being blocked, or
    the network. A misconfiguration fails every route identically, so paying
    Bright Data to reproduce it is waste: `--race` with a misspelt --model
    started a collector run before this check existed.
    """
    if looks_throttled(reason) or any(m in reason for m in REFUSED_MARKS):
        return True
    if any(mark in reason for mark in
           ("Timeout", "ConnectionError", "SSLError", "blocked")):
        return True
    # A route that ran and came back short -- "no rows", "only 600/1130 cars".
    return reason.startswith(("no rows", "only "))


def read_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A missing or corrupt state file means "nothing known", which is the
        # same as a first run. It must never be the reason a run fails.
        return {}


def write_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def park(state: Dict[str, Any], name: str, reason: str,
         default_cooldown: float) -> None:
    """Remember that `name` just failed, and for how long that matters."""
    refused = any(mark in reason for mark in REFUSED_MARKS)
    state[name] = {
        "at": time.time(),
        "cooldown": ZONE_REFUSED_COOLDOWN if refused else default_cooldown,
        "why": "account refused (no calls left / no KYC)" if refused
                 else "throttled",
    }
    write_state(state)


def parked_for(state: Dict[str, Any], name: str,
               default_cooldown: float) -> Optional[str]:
    """How much of `name`'s cooldown is left, as text, or None if it is clear."""
    entry = state.get(name)
    if isinstance(entry, (int, float)):  # written by an older version
        entry = {"at": entry, "cooldown": default_cooldown, "why": "throttled"}
    if not isinstance(entry, dict):
        return None

    at = entry.get("at")
    if not isinstance(at, (int, float)):
        return None

    cooldown = entry.get("cooldown", default_cooldown)
    if not isinstance(cooldown, (int, float)):
        cooldown = default_cooldown
    left = cooldown - (time.time() - at)
    if left <= 0:
        return None
    unit = f"{left / 3600:.0f}h" if left > 3600 else f"{left:.0f}s"
    return f"{entry.get('why', 'failed')} {time.time() - at:.0f}s ago, {unit} left"


def load_trigger_collector():
    """brightdata_collector/ is a directory of scripts, not a package."""
    path = os.path.join(ROOT_DIR, "brightdata_collector", "trigger_collector.py")
    spec = importlib.util.spec_from_file_location("trigger_collector", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── The routes ────────────────────────────────────────────────────────────────

def run_collector(model_key: str, pages: Optional[List[int]], limit: int,
                  timeout: float, max_pages: int, cache=None) -> Dict[str, Any]:
    """The hosted collector, reshaped into this project's row shape.

    It walks pages itself from page 1, so a request for pages 3-5 becomes
    max_page 5 and the extra pages are dropped afterwards. Its rows carry no
    page number, and the car id is only in the detail URL.

    `--pages all` cannot be honoured here. The collector is told a page count
    up front, and it renders every list and detail page in a real browser --
    measured at 85s for one page and 231s for ten, so this model's 57 pages
    would be around a quarter of an hour. It is capped at `max_pages` and the
    result is marked `partial`, rather than quietly standing in for a full run.
    """
    tc = load_trigger_collector()
    spec = load_model_spec(model_key)

    partial = False
    if pages is None:
        max_page, partial = max_pages, True
        log(f"  note: --pages all falls back to the first {max_page} page(s) "
            f"here -- the collector renders every page in a browser")
    else:
        max_page = max(pages)

    started = time.time()
    session = tc.make_session()
    url = search_page_url(model_query(spec), 1, limit)
    collection_id = tc.trigger_run(session, tc.DEFAULT_COLLECTOR, url, max_page)
    log(f"  collector run {collection_id}, max_page {max_page}")

    raw = tc.wait_for_rows(session, collection_id, timeout)
    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        url_value = str(item.get("product_page_url") or "")
        car_id = url_value.rstrip("/").rsplit("/", 1)[-1] or None
        # The collector's rows carry no page number -- it walks the pages
        # itself and hands back one flat list. The list is in the order it
        # rendered them, which is page order, so the page is recoverable from
        # the position. Derived, not reported, but better than a null that
        # makes the rows impossible to line up against another route's.
        rows.append(row(index // limit + 1, car_id,
                        item.get("car_number") or None,
                        item.get("model_info") or ""))
        if cache is not None:
            cache.remember(car_id, item.get("car_number"),
                           item.get("model_info"), model_key)

    elapsed = time.time() - started
    return {
        "rows": rows,
        "model": model_key,
        "label": spec.get("label"),
        "source": "collector",
        "transport": "brightdata-collector",
        # True when the run was asked for every page and could not walk them
        # all -- the rows are the first `max_page` pages, not the model.
        "partial": partial,
        "collectionId": collection_id,
        "pages": sorted({r["page"] for r in rows}),
        "carNumbersFilled": sum(1 for r in rows if r["car_number"]),
        "elapsedSeconds": round(elapsed, 2),
    }


def rescale_pages(pages: Optional[List[int]], from_limit: int,
                  to_limit: int) -> Optional[List[int]]:
    """The same cars, numbered for a different page size.

    The API routes read 200 cars a page and the browser routes 20, so `--pages
    2` means two different sets of cars to them. Asked for every page there is
    nothing to translate; asked for specific ones, each is expanded into the
    pages that cover the same cars.
    """
    if pages is None or from_limit == to_limit:
        return pages
    wanted = set()
    for page in pages:
        first_car = (page - 1) * from_limit
        last_car = page * from_limit - 1
        wanted.update(range(first_car // to_limit + 1, last_car // to_limit + 2))
    return sorted(wanted)


def build_ladder(args, pages: Optional[List[int]], cache) -> List[tuple]:
    """(name, why-skipped-or-None, callable) in the order they are tried."""
    bd = brightdata_available()
    no_bd = "no Bright Data credentials in .env"
    sb = scrapingbee_available()
    no_sb = "no SCRAPINGBEE_KEY in .env"
    page_route_pages = rescale_pages(pages, args.limit, PAGE_ROUTE_LIMIT)

    return [
        ("api-direct", None, lambda: export_via_api(
            args.model, args.section, pages, args.limit, args.workers, "direct",
            with_detail=True, delay=args.delay, cache=cache)),
        # The same JSON calls, through someone else's IPs. Both belong ahead of
        # the browser routes: still JSON, 0.9 KB a car, no rendering, and a
        # different exit IP is exactly what an IP-level block needs.
        #
        # ScrapingBee first because it works today -- verified end to end, and
        # its classic tier (1 credit, no premium proxy) gets through, which
        # Bright Data's datacenter IPs do not. Its own cap is concurrency, not
        # requests: the plan allows about 3 at a time, so it is slow but steady.
        ("api-scrapingbee", None if sb else no_sb, lambda: export_via_api(
            args.model, args.section, pages, args.limit, args.workers,
            "scrapingbee", with_detail=True, delay=args.delay, cache=cache)),
        # Bright Data answers 402 on every call until the zone passes KYC.
        ("api-brightdata", None if bd else no_bd, lambda: export_via_api(
            args.model, args.section, pages, args.limit, args.workers,
            "brightdata", with_detail=True, delay=args.delay, cache=cache)),
        ("page-local", None, lambda: export_via_browser(
            args.model, args.section, page_route_pages, PAGE_ROUTE_LIMIT,
            args.workers, with_detail=True, delay=args.delay, browser="local",
            cache=cache)),
        ("collector", None if bd else no_bd, lambda: run_collector(
            args.model, page_route_pages, PAGE_ROUTE_LIMIT, args.timeout,
            args.collector_max_page, cache)),
    ]


def verdict(run: Dict[str, Any], min_fill: float) -> Optional[str]:
    """None when the run is good enough to keep; otherwise why it is not.

    Two ways a route can come back short, and both have to be checked against
    what was asked for rather than against what arrived:

      missing cars    a listing page was throttled and its ~20 cars are simply
                      absent. Every other count is computed over the rows that
                      did land, so this one is invisible unless asked for.
      empty numbers   the rows are there but the detail calls failed.
    """
    rows = run.get("rows") or []
    if not rows:
        return "no rows"

    expected = run.get("expectedCars") or 0
    if expected and len(rows) < expected * (1 - MISSING_TOLERANCE):
        failed = run.get("pagesFailed") or []
        return (f"only {len(rows)}/{expected} cars"
                + (f", {len(failed)} listing page(s) never landed" if failed else ""))

    filled = sum(1 for r in rows if r.get("car_number"))
    if filled < len(rows) * min_fill:
        return f"only {filled}/{len(rows)} car numbers (< {min_fill:.0%})"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Encar listings: car number + model info, by the fastest "
                    "route that works.")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"models.json key (default: {DEFAULT_MODEL})")
    parser.add_argument("--section", choices=SECTIONS, default=DEFAULT_SECTION,
                        help=f"listing tab: general = 일반등록 "
                             f"(default: {DEFAULT_SECTION})")
    parser.add_argument("--pages", default="all",
                        help="page number, range or all -- e.g. 1, 2-5, all "
                             "(default: all -- measured at 33s for the whole "
                             "1130-car model on api-direct)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"cars per listing request on the API routes "
                             f"(default: {DEFAULT_LIMIT}). The browser routes "
                             f"keep the site's own {PAGE_ROUTE_LIMIT} a page, "
                             f"and --pages is translated between the two")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"requests in flight (default: {DEFAULT_WORKERS})")
    parser.add_argument("--delay", type=float, default=None,
                        help=f"seconds to pause between chunks of requests. "
                             f"Left out, it is worked out from the size of the "
                             f"run: nothing for a run that fits inside Encar's "
                             f"budget of about {RATE_BUDGET:.0f} requests, and "
                             f"enough to hold ~{SAFE_RATE:.0f} requests/second "
                             f"for one that does not. 0 disables it")
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES),
                        help="routes to try, fastest first (default: "
                             + ",".join(DEFAULT_STRATEGIES) + ")")
    parser.add_argument("--min-fill", type=float, default=DEFAULT_MIN_FILL,
                        help=f"fraction of car numbers a route must fill to "
                             f"count as a result (default: {DEFAULT_MIN_FILL})")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="seconds to wait for the hosted collector "
                             "(default: 600)")
    parser.add_argument("--collector-max-page", type=int,
                        default=COLLECTOR_MAX_PAGE,
                        help=f"pages the collector walks when --pages is all. "
                             f"It renders every page in a browser (~85s a "
                             f"page), so a full model is not practical there "
                             f"(default: {COLLECTOR_MAX_PAGE})")
    parser.add_argument("--cache-file", default=DEFAULT_CACHE_FILE,
                        help=f"where car numbers are remembered between runs "
                             f"(default: "
                             f"{os.path.relpath(DEFAULT_CACHE_FILE, ROOT_DIR)})")
    parser.add_argument("--cache-max-age", type=float, default=CACHE_MAX_AGE_DAYS,
                        help=f"days before a cached car number is looked up "
                             f"again (default: {CACHE_MAX_AGE_DAYS:.0f}). The "
                             f"cache cannot tell when an entry is wrong, so "
                             f"this is the only thing that ever corrects one. "
                             f"0 disables the limit")
    parser.add_argument("--no-cache", action="store_true",
                        help="ask Encar for every car number, even ones already "
                             "known. A vehicle number never changes, so this is "
                             "for verifying the cache, not for normal runs")
    parser.add_argument("--cooldown", type=float, default=THROTTLE_COOLDOWN,
                        help=f"skip a route this many seconds after it was "
                             f"last throttled; 0 disables the memory "
                             f"(default: {THROTTLE_COOLDOWN:.0f})")
    parser.add_argument("--race", action="store_true",
                        help="when a route fails, start the collector in the "
                             "background and keep trying the cheaper routes. "
                             "Cuts the worst case from ~163s to ~90s, at the "
                             "cost of paying for a collector run that may not "
                             "be used")
    parser.add_argument("--json", dest="json_out",
                        help="write the run to this JSON file")
    parser.add_argument("--csv", dest="csv_out",
                        help="write the rows to this CSV file")
    parser.add_argument("--out-dir", nargs="?", const=DEFAULT_OUT_DIR,
                        default=DEFAULT_OUT_DIR,
                        help=f"write both, named <model>_<timestamp>, in this "
                             f"directory (default: "
                             f"{os.path.relpath(DEFAULT_OUT_DIR, ROOT_DIR)})")
    parser.add_argument("--print", dest="print_json", action="store_true",
                        help="also dump the whole JSON document to stdout. Off "
                             "by default: a full run is 1130 rows, and the "
                             "files are written either way")
    args = parser.parse_args()

    pages = parse_pages(args.pages)

    wanted = [s.strip() for s in args.strategies.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in ALL_STRATEGIES]
    if unknown:
        raise SystemExit(f"Unknown strategy: {', '.join(unknown)}. "
                         f"Known: {', '.join(ALL_STRATEGIES)}")

    cache = PlateCache(args.cache_file, enabled=not args.no_cache,
                       model_key=args.model,
                       max_age_days=args.cache_max_age).load()
    if cache.enabled and cache.entries:
        log(f"  cache: {len(cache.entries)} car number(s) known from earlier runs")

    # `--delay` left out means "work it out", and the size of the run is only
    # known once the first listing call reports Count -- so the decision is
    # made down in export_listings, per phase, rather than guessed here. It
    # says so in the log only when it actually decides to slow down.

    ladder = [step for step in build_ladder(args, pages, cache)
              if step[0] in wanted]
    attempts: List[Dict[str, Any]] = []
    run: Optional[Dict[str, Any]] = None
    used = ""

    state = read_state()
    raced: Optional[BackgroundRoute] = None
    # The fullest run seen so far that did not clear the bar, as
    # (run, strategy, why). Better than nothing when every route falls short.
    best: Optional[tuple] = None

    for index, (name, skip, call) in enumerate(ladder):
        # A route still inside its cooldown is skipped rather than run: the
        # block is a time window on this host, and asking again inside it both
        # wastes the attempt and holds the window open.
        left = (None if args.cooldown <= 0
                else parked_for(state, name, args.cooldown))
        if left and not skip:
            skip = left

        if skip:
            log(f"[{name}] skipped: {skip}")
            attempts.append({"strategy": name, "result": f"skipped: {skip}"})
            continue

        # The collector spends its ~85s waiting on Bright Data's side, not on
        # this host's network, so it can be started the moment a route fails
        # and left to run while the next one is tried. Whichever answers first
        # wins. It is opt-in because a started run is billed even if the
        # cheaper route wins the race.
        if raced is not None and raced.name == name:
            log(f"[{name}] waiting on the run started {time.time() - raced.started:.0f}s ago")
            candidate, reason = raced.result()
            raced = None
        else:
            log(f"[{name}] trying")
            started = time.time()
            try:
                candidate, reason = call(), None
            # A route fails in whatever way its own layer fails: SystemExit is
            # how export_listings reports a throttled first page, RuntimeError
            # is a missing transport, and the browser and collector paths raise
            # their own. None of them should end the run while a route is left.
            except (SystemExit, Exception) as exc:
                candidate, reason = None, f"{type(exc).__name__}: {exc}".strip()
                log(f"[{name}] failed after {time.time() - started:.1f}s: {reason}")

        if candidate is not None:
            bad = verdict(candidate, args.min_fill)
            if bad:
                log(f"[{name}] dropped: {bad}")
                # Short is not the same as worthless. A route that came back
                # with 1020 of 1130 cars still beats the collector's 186, so it
                # is kept aside: if nothing better lands, it is the answer, and
                # the run says plainly that it is incomplete.
                if not best or len(candidate["rows"]) > len(best[0]["rows"]):
                    best = (candidate, name, bad)
                candidate, reason = None, bad

        if candidate is None:
            attempts.append({"strategy": name, "result": reason or "failed"})
            if reason and (looks_throttled(reason)
                           or any(m in reason for m in REFUSED_MARKS)):
                park(state, name, reason, args.cooldown)
            if args.race and raced is None and worth_racing(reason or ""):
                raced = start_race(ladder, index, log)
            continue

        state.pop(name, None)
        write_state(state)
        attempts.append({"strategy": name, "result": "ok"})
        run, used = candidate, name
        log(f"[{name}] ok -- {len(candidate['rows'])} rows")
        if raced is not None:
            log(f"  note: the {raced.name} run started in parallel is billed "
                f"whether or not it is used")
            attempts.append({"strategy": raced.name,
                             "result": "started in parallel, not needed"})
        break

    if run is None and best is not None:
        run, used, why = best
        run["incomplete"] = why
        log(f"[{used}] using it anyway -- {len(run['rows'])} rows, {why}. "
            f"Nothing better landed; the output is marked incomplete.")

    if run is None:
        for attempt in attempts:
            log(f"  {attempt['strategy']}: {attempt['result']}")
        raise SystemExit("Every route failed. Encar is likely rate limiting "
                         "this host -- wait a few minutes and run it again.")

    rows = run.pop("rows")
    run["strategy"] = used
    run["attempts"] = attempts

    # A complete run over every page is the only time the cache can tell a
    # delisted car from one that simply was not asked about, so it is the only
    # time entries are dropped. Anything narrower leaves the file alone.
    if (pages is None and run.get("complete") and not run.get("incomplete")
            and not args.no_cache):
        gone = cache.prune_to((r["car_id"] for r in rows), args.model)
        if gone:
            log(f"  cache: dropped {gone} car(s) no longer listed")

    saved = cache.save()
    if saved:
        log(f"  cache: {cache.added} new, {len(cache.entries)} known in total")

    if run.get("complete") is False or run.get("incomplete") or run.get("partial"):
        log(f"  ! this run is incomplete -- see `complete` / `missingCars` "
            f"in the output before using it as a full picture")

    for path in results.save(TOOL, args.model, rows, run, json_path=args.json_out,
                             csv_path=args.csv_out, out_dir=args.out_dir):
        log(f"  wrote {path}")

    if args.print_json:
        json.dump(results.envelope(TOOL, args.model, rows, run), sys.stdout,
                  ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
