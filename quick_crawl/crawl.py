#!/usr/bin/env python3
"""
crawl.py -- Encar listings by page, car number + model info only.

    python quick_crawl/crawl.py                    # 일반등록, page 1, 20 cars
    python quick_crawl/crawl.py --pages 3
    python quick_crawl/crawl.py --pages 2-5 --csv out.csv
    python quick_crawl/crawl.py --pages all --workers 4 --delay 1
    python quick_crawl/crawl.py --section premium --transport brightdata

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

Where the speed comes from
--------------------------
- `include=CATEGORY` on the detail call. The full payload is ~11 KB; with this
  it is ~0.9 KB and still carries vehicleNo and the whole category block. That
  is 12x less to transfer and parse, on the 20 requests that dominate the run.
- The detail calls go out as one batch on a small thread pool over a pooled,
  cookie-less Session, so the TLS handshakes happen once and get reused.
- No cookie warm-up. The old crawler opens encar.com first to look like a
  browser; the API answers an anonymous request with no headers at all, so that
  round trip buys nothing here.
- No database, no browser on the normal path, nothing beyond `requests`.

Bright Data is the fallback, not the default: see transport.py. With
`--transport auto` it is reached only after Encar throttles the run.

Nothing is written anywhere: the result goes to stdout as JSON, or to --csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
load_dotenv(os.path.join(ROOT, ".env"))

sys.path.insert(0, HERE)
from transport import (  # noqa: E402
    Req, Result, brightdata_available, make_client,
)

# The two listing sections the site shows as tabs. 일반등록 is `general`;
# `premium` is the paid-placement tab. Both cover the same set -- the Count is
# identical -- and differ in ordering, so the section is a view, not a filter.
SEARCH_URL = "https://api.encar.com/search/car/list/{section}"
SECTIONS = ("general", "premium")
DEFAULT_SECTION = "general"

DETAIL_URL = "https://api.encar.com/v1/readside/vehicle/{car_id}"
MODELS_FILE = os.path.join(ROOT, "models.json")

DEFAULT_MODEL = "bmw_3series_g20"
DEFAULT_LIMIT = 20
DEFAULT_WORKERS = 8

# Detail calls are issued in chunks so a long run reports progress and has a
# place to pace itself. One page's worth of cars is far below this.
DETAIL_CHUNK = 100

# Encar's rate limit needs real time to clear from this host, so a retry a
# second later just burns the attempt. Escalating to Bright Data is both faster
# and likelier to work; the wait is what is left when there is nothing to
# escalate to.
THROTTLE_BACKOFF = 20.0

# On Bright Data the picture is different. CloudFront throttles its exit IPs
# individually and unpredictably -- measured over repeated runs, the search
# endpoint came back 407 on roughly two attempts in three. Each batch opens a
# fresh CDP connection and so tends to land on a different exit IP, which makes
# retrying there cheap and worthwhile where waiting is not.
MAX_THROTTLE_ROUNDS = 3
BD_RETRY_PAUSE = 2.0


def log(msg: str) -> None:
    """Progress goes to stderr so stdout stays a clean JSON document."""
    print(msg, file=sys.stderr, flush=True)


def load_spec(key: str) -> Dict[str, str]:
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


def build_query(spec: Dict[str, str]) -> str:
    """Encar's search expression for one model.

    `Hidden.N` restricts it to listings currently advertised. `car_type` is the
    domestic flag -- Y for Korean makes, N for imports -- and defaults to N,
    matching the existing entries. The client does the URL-encoding.
    """
    return (
        "(And.Hidden.N._.(C.CarType.{car_type}._.(C.Manufacturer.{manufacturer}._."
        "(C.ModelGroup.{model_group}._.Model.{model}.))))"
    ).format(**{"car_type": "N", **spec})


class Fetcher:
    """Runs batches, and moves the whole run to Bright Data when throttled.

    Escalation is one-way. Once Encar has rate limited this host, going back to
    direct would only walk into the same limit, so the remaining batches stay
    on the fallback for the rest of the run.
    """

    def __init__(self, mode: str, workers: int):
        self.mode = mode
        self.workers = workers
        self.escalated = mode == "brightdata"
        self.client = make_client(
            "brightdata" if mode == "brightdata" else "direct", workers
        )

    def _escalate(self) -> bool:
        if self.escalated or self.mode != "auto" or not brightdata_available():
            return False
        self.client.close()
        self.client = make_client("brightdata", self.workers)
        self.escalated = True
        return True

    def run(self, reqs: Sequence[Req]) -> List[Result]:
        """One batch, re-running whatever Encar throttled until it lands."""
        results = self.client.get_many(reqs)

        for _ in range(MAX_THROTTLE_ROUNDS):
            throttled = [i for i, r in enumerate(results) if r.throttled]
            if not throttled:
                break

            if self._escalate():
                log(f"    throttled on {len(throttled)} request(s) "
                    f"-> switching to {self.client.name}")
            elif self.escalated:
                # The connection is held across batches for speed, so getting a
                # different exit IP means asking for a new one explicitly.
                reconnect = getattr(self.client, "reconnect", None)
                if reconnect is not None:
                    reconnect()
                log(f"    {self.client.name}: {len(throttled)} request(s) got "
                    f"407, retrying on a fresh exit IP")
                time.sleep(BD_RETRY_PAUSE)
            else:
                log(f"    throttled on {len(throttled)} request(s), "
                    f"waiting {THROTTLE_BACKOFF:.0f}s")
                time.sleep(THROTTLE_BACKOFF)

            # Only the throttled ones are worth repeating; the rest landed.
            retry = self.client.get_many([reqs[i] for i in throttled])
            for i, result in zip(throttled, retry):
                results[i] = result

        return results

    @property
    def name(self) -> str:
        return self.client.name + (" (after throttle)" if self.escalated
                                   and self.mode == "auto" else "")

    def close(self) -> None:
        self.client.close()


def model_info(category: Dict[str, Any], fallback: str) -> str:
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


def listing_model(item: Dict[str, Any]) -> str:
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


def throttle_hint(fetcher: "Fetcher", result: Result) -> str:
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


def collect_listings(fetcher: "Fetcher", section: str, query: str,
                     pages: Optional[List[int]], limit: int) -> List[Dict[str, Any]]:
    """Listing items for the requested pages, in page order.

    The first page is fetched on its own because it is what reports `Count`,
    and with `--pages all` that Count is what says how many pages there are.
    The rest go out as one batch: they are independent requests and there is
    nothing to gain by walking them one at a time.
    """
    first_page = 1 if pages is None else pages[0]
    first = fetcher.run([listing_request(section, query, first_page, limit, True)])[0]
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

    rest = [p for p in pages if p != first_page]
    if rest:
        results = fetcher.run([
            listing_request(section, query, p, limit, False) for p in rest
        ])
        for page_no, result in zip(rest, results):
            if not result.ok:
                log(f"    ! page {page_no}: {result.error}")
                continue
            page_items = (result.data or {}).get("SearchResults") or []
            for it in page_items:
                it["_page"] = page_no
            items.extend(page_items)

    log(f"  listing: {len(items)} cars from {len(pages)} page(s) "
        f"of {total} total")
    return items


def collect_details(fetcher: "Fetcher", items: List[Dict[str, Any]],
                    delay: float) -> List[Dict[str, Any]]:
    """Car number and model info per listing, in chunks so progress is visible."""
    rows: List[Dict[str, Any]] = []

    for start in range(0, len(items), DETAIL_CHUNK):
        chunk = items[start:start + DETAIL_CHUNK]
        results = fetcher.run([
            (DETAIL_URL.format(car_id=it.get("Id")), {"include": "CATEGORY"})
            for it in chunk
        ])

        for item, detail in zip(chunk, results):
            fallback = listing_model(item)
            data = detail.data or {}
            # A failed detail call leaves car_number None rather than dropping
            # the row, so the output always matches the pages that were asked
            # for -- a missing number is visible instead of silently absent.
            if not detail.ok and not detail.gone:
                log(f"    ! detail {item.get('Id')}: {detail.error}")
            rows.append({
                "page": item.get("_page"),
                "car_id": str(item.get("Id")),
                "car_number": data.get("vehicleNo") or None,
                "model_info": (model_info(data.get("category") or {}, fallback)
                               if detail.ok else fallback),
            })

        if len(items) > DETAIL_CHUNK:
            done = min(start + DETAIL_CHUNK, len(items))
            log(f"    detail {done}/{len(items)}")
            if delay and done < len(items):
                time.sleep(delay)

    return rows


def crawl_pages(model_key: str, section: str, pages: Optional[List[int]],
                limit: int, workers: int, with_detail: bool = True,
                delay: float = 0.0, browser: str = "local") -> Dict[str, Any]:
    """`--source page`: read the site the way a browser does. See page_source.py.

    The listing comes from the search page's own XHR and the car number from
    the detail page's HTML, so no Encar API is called directly. This needs the
    Scraping Browser -- there is nothing to render with otherwise.
    """
    from page_source import PageCrawler
    from transport import brightdata_ws_url, brightdata_proxy_url

    spec = load_spec(model_key)
    action = build_query(spec)
    proxy = brightdata_proxy_url()
    crawler = PageCrawler(
        ws_url=brightdata_ws_url(), workers=workers, log=log,
        browser=browser, proxy_url=proxy,
    )
    started = time.time()

    try:
        log(f"Model {model_key} ({spec.get('label', '?')}), section {section}, "
            f"source page (listing: {browser} browser, "
            f"detail: {'residential proxy' if proxy else 'direct'})")

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

            data = crawler.listing(action, page_no, limit, section)
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

            if with_detail:
                found = crawler.details(items)
            else:
                found = [(None, listing_model(it)) for it in items]

            for item, (car_number, info) in zip(items, found):
                rows.append({
                    "page": page_no,
                    "car_id": str(item.get("Id")),
                    "car_number": car_number,
                    "model_info": info,
                })

            if delay and queue:
                time.sleep(delay)

        elapsed = time.time() - started
        filled = sum(1 for r in rows if r["car_number"])
        log(f"  {filled}/{len(rows)} car numbers in {elapsed:.2f}s (page source)")

        return {
            "model": model_key,
            "label": spec.get("label"),
            "section": section,
            "source": "page",
            "pages": sorted({r["page"] for r in rows}),
            "count": len(rows),
            "transport": f"{browser}-browser",
            "elapsed_seconds": round(elapsed, 2),
            "rows": rows,
        }
    finally:
        crawler.close()


def crawl(model_key: str, section: str, pages: Optional[List[int]], limit: int,
          workers: int, mode: str, with_detail: bool = True,
          delay: float = 0.0) -> Dict[str, Any]:
    spec = load_spec(model_key)
    fetcher = Fetcher(mode, workers)
    started = time.time()

    try:
        log(f"Model {model_key} ({spec.get('label', '?')}), "
            f"section {section}, transport {fetcher.name}")

        items = collect_listings(fetcher, section, build_query(spec), pages, limit)

        if with_detail:
            rows = collect_details(fetcher, items, delay)
        else:
            # Without the detail pass there is no car number to be had; the
            # listing carries the model only.
            rows = [{
                "page": it.get("_page"),
                "car_id": str(it.get("Id")),
                "car_number": None,
                "model_info": listing_model(it),
            } for it in items]

        elapsed = time.time() - started
        filled = sum(1 for r in rows if r["car_number"])
        log(f"  {filled}/{len(rows)} car numbers in {elapsed:.2f}s ({fetcher.name})")

        return {
            "model": model_key,
            "label": spec.get("label"),
            "section": section,
            "source": "api",
            "pages": sorted({r["page"] for r in rows}),
            "count": len(rows),
            "transport": fetcher.name,
            "elapsed_seconds": round(elapsed, 2),
            "rows": rows,
        }
    finally:
        fetcher.close()


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    # utf-8-sig so Excel opens the Korean plates and model names correctly.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["page", "car_id", "car_number", "model_info"])
        writer.writeheader()
        writer.writerows(rows)


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
    parser.add_argument("--delay", type=float, default=0.0,
                        help="seconds to pause between detail chunks on a long run")
    parser.add_argument("--no-detail", action="store_true",
                        help="skip the per-car requests -- fast, but no car number")
    parser.add_argument("--source", choices=("page", "api"), default="page",
                        help="page (default) renders the real search page in the "
                             "Scraping Browser and reads its own XHR, then takes "
                             "the car number from the detail page HTML; api calls "
                             "api.encar.com directly")
    parser.add_argument("--browser", choices=("local", "brightdata"),
                        default="local",
                        help="--source page only: which browser renders the "
                             "listing. local is the default because Bright Data "
                             "cannot load the site's JavaScript (see README)")
    parser.add_argument("--transport", choices=("auto", "direct", "brightdata"),
                        default="brightdata",
                        help="brightdata (default) routes every request through "
                             "Bright Data; direct goes straight out; auto goes "
                             "direct and fails over when throttled")
    parser.add_argument("--csv", help="also write the rows to this CSV file")
    args = parser.parse_args()

    pages = parse_pages(args.pages)

    if args.transport != "direct" and not brightdata_available():
        log("  note: no Bright Data credentials in .env, direct only")

    try:
        if args.source == "page":
            result = crawl_pages(args.model, args.section, pages, args.limit,
                                 args.workers, with_detail=not args.no_detail,
                                 delay=args.delay, browser=args.browser)
        else:
            result = crawl(args.model, args.section, pages, args.limit,
                           args.workers, args.transport,
                           with_detail=not args.no_detail, delay=args.delay)
    except RuntimeError as exc:  # transport misconfiguration
        raise SystemExit(str(exc))

    if args.csv:
        write_csv(args.csv, result["rows"])
        log(f"  wrote {args.csv}")

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
