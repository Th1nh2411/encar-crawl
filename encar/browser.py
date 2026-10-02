"""
browser.py -- Read Encar the way a browser does, not by calling the API.

The `page-local` route. Two steps:

  listing  load the real search page in a local Chromium and read the XHR it
           fires for itself
  detail   fetch the car's detail page over plain HTTP and pull the JSON
           embedded in its HTML (parse.detail_fields_from_html)

Why it is shaped this way
-------------------------
The search URL carries its state in a hash fragment (`#!{...,"page":2,...}`),
and a hash is never sent to the server. Fetching that URL over HTTP therefore
returns the same app shell every time -- 227 KB of JavaScript with no car in it
(`carid` appears zero times). Only the page's own JavaScript turns that hash
into a request, so the listing has to come from a real browser. What the page
asks for is `/search/car/list/general` -- the 일반등록 tab -- and intercepting
that response gives the same rows the API would, with the page building the
query, the headers and the paging itself.

The detail page is the opposite: it is server-rendered and already carries the
whole vehicle record as JSON in the HTML, `vehicleNo` included. No rendering is
needed, so it is fetched as plain HTML through the same browser.

Reliability, measured
---------------------
When Encar does not like a client, the page still loads 200 but fires no XHR
at all and throws a JavaScript alert -- that is the signature to look for, not
an HTTP error. The listing step retries on a fresh browser session.

Both steps go out from this host, unproxied. Routing them through a Bright
Data zone was tried and removed: without KYC the zone enforces Encar's
robots.txt -- `www.encar.com` says `Disallow: /js/`, so the site's JavaScript
never boots, and `fem.encar.com` bars `/cars/detail/` -- and every request came
back 402. The Scraping Browser zone loads the JavaScript, but Encar serves its
datacenter exit IPs nothing.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, parse_qs, unquote

from .parse import DETAIL_PAGE, Row, apply_detail, detail_fields_from_html

SEARCH_PAGE = "https://www.encar.com/fc/fc_carsearchlist.do?carType=for"

# The listing XHR, told apart from the promo carousels the page also loads by
# its `sr` parameter -- those ask for their own offsets and counts.
LISTING_PATH = "/search/car/list/"

# www.encar.com serves EUC-KR; fem.encar.com serves UTF-8.
ENCODINGS = ("utf-8", "euc-kr", "cp949")


def decode(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def search_page_url(action: str, page: int, limit: int,
               sort: str = "ModifiedDate") -> str:
    """The site's own URL for one page of results.

    The hash is the site's state object, written exactly as the page writes it
    -- `page` is 1-based here, and the page turns it into Encar's offset.
    """
    state = {
        "action": action,
        "toggle": {},
        "layer": "",
        "sort": sort,
        "page": page,
        "limit": limit,
        "searchKey": "",
        "loginCheck": False,
    }
    # The hash has to be spelled the way the site spells it, or its router
    # never recognises it and the page loads without ever running the search:
    # no separator spaces, and the parentheses of the query left literal.
    # `json.dumps` defaults to ", " and ": ", which is enough to break it.
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    return f"{SEARCH_PAGE}#!" + quote(encoded, safe="()")


def listing_score(url: str, rows: int, page: int, limit: int,
                  section: Optional[str]) -> int:
    """How well one `/search/car/list/` response matches the page we asked for.

    The search page fires several of these: the result list plus promo blocks
    that carry their own offsets and much smaller counts. Scoring rather than
    filtering means a change in how the page spells `sr` costs accuracy, not
    the whole run -- the best of what arrived is still returned.
    """
    score = 0
    sr = unquote(parse_qs(urlsplit(url).query).get("sr", [""])[0])
    if sr.endswith(f"|{(page - 1) * limit}|{limit}"):
        score += 100
    if section and url.split(LISTING_PATH)[1].startswith(section):
        score += 10
    if rows == limit:
        score += 5
    return score


class BrowserSource:
    """Drives one browser session for the listing, and an HTTP pool for details.

    The browser, the Playwright driver and the event loop are created once and
    kept. The session is thrown away on purpose when the listing step is
    blocked, because a fresh one is what a retry needs.
    """

    # When the listing XHR comes at all it lands within a few seconds of
    # navigation -- measured at 2.0s to 6.4s. Waiting longer only makes a
    # blocked attempt expensive, and being blocked is the common case: 8s is
    # the measured ceiling plus room, and it is the difference between a
    # blocked page costing 20s and costing 8s, twice per run.
    XHR_TIMEOUT = 8.0

    def __init__(self, workers: int = 8, timeout: float = 60.0, log=print):
        self.workers = workers
        self.timeout = timeout
        self.log = log
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pw = None
        self._browser = None
        self._session = None

    # ── session ───────────────────────────────────────────────────────────

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop

    async def _ctx(self):
        from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._browser is None:
            # Local Chromium, on this host's own IP, deliberately unproxied --
            # see the module docstring for why a proxy zone cannot do this.
            self._browser = await self._pw.chromium.launch()

        browser = self._browser
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context(
            locale="ko-KR"
        )
        # Encar pops an alert when it does not like the client. Left unhandled
        # it blocks every later call on the page.
        ctx.on("dialog", lambda d: asyncio.create_task(_dismiss(d)))
        return ctx

    async def _drop_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # the session is going away regardless
                pass
            self._browser = None

    # ── listing ───────────────────────────────────────────────────────────

    def listing_page(self, action: str, page: int, limit: int, section: Optional[str],
                attempts: int = 3) -> Optional[Dict[str, Any]]:
        """The listing payload for one page, or None if every attempt was blocked."""
        for attempt in range(attempts):
            data = self._get_loop().run_until_complete(
                self._listing_page_once(action, page, limit, section)
            )
            if data is not None:
                return data
            if attempt < attempts - 1:
                self.log(f"    page {page}: no listing XHR "
                         f"(blocked), retrying on a fresh browser")
                self._get_loop().run_until_complete(self._drop_browser())
                time.sleep(2.0)
        return None

    async def _listing_page_once(self, action: str, page: int, limit: int,
                            section: Optional[str]) -> Optional[Dict[str, Any]]:
        try:
            ctx = await self._ctx()
        except Exception as exc:
            self.log(f"    connect failed: {exc}")
            return None

        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        loop = asyncio.get_event_loop()
        found: asyncio.Future = loop.create_future()
        # Every listing response seen, so a near-miss still beats nothing.
        candidates: List[Tuple[int, Dict[str, Any]]] = []

        async def on_response(resp) -> None:
            if LISTING_PATH not in resp.url:
                return
            try:
                data = await resp.json()
            except Exception:
                return
            rows = data.get("SearchResults")
            if rows is None:
                return
            score = listing_score(resp.url, len(rows), page, limit, section)
            candidates.append((score, data))
            # An exact match is the one we came for; stop waiting on it.
            if score >= 100 and not found.done():
                found.set_result(data)

        handler = lambda r: asyncio.create_task(on_response(r))  # noqa: E731
        pg.on("response", handler)
        try:
            # "commit" returns as soon as navigation starts. Waiting for load
            # or networkidle is pointless here: the page keeps analytics and
            # polling traffic alive, so neither ever settles, and the XHR we
            # want arrives long before they would.
            await pg.goto(search_page_url(action, page, limit), timeout=self.timeout * 1000,
                          wait_until="commit")
        except Exception as exc:
            self.log(f"    page {page}: navigation failed: {str(exc)[:80]}")
            pg.remove_listener("response", handler)
            return None

        try:
            return await asyncio.wait_for(found, timeout=self.XHR_TIMEOUT)
        except asyncio.TimeoutError:
            if not candidates:
                return None
            score, data = max(candidates, key=lambda c: c[0])
            self.log(f"    page {page}: no exact listing XHR, using the closest "
                     f"of {len(candidates)} (score {score}, "
                     f"{len(data.get('SearchResults') or [])} rows)")
            return data
        finally:
            pg.remove_listener("response", handler)

    # ── detail ────────────────────────────────────────────────────────────

    def fill_details(self, rows: List[Row]) -> int:
        """Fill rows in place from their detail pages. Returns how many got a number.

        This is plain HTTP on a thread pool, not the browser: the detail page is
        server-rendered and already carries the vehicle record as JSON in its
        markup, so there is nothing to execute.
        """
        if not rows:
            return 0

        import concurrent.futures

        session = self._http()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.workers, len(rows))
        ) as pool:
            found = list(pool.map(lambda r: self._detail(session, r["car_id"]), rows))

        for row, fields in zip(rows, found):
            if fields:
                apply_detail(row, fields)
        return sum(1 for r in rows if r.get("vehicle_no"))

    def _http(self):
        if self._session is None:
            import requests
            import requests.adapters

            session = requests.Session()
            session.trust_env = False
            session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            })
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.workers, pool_maxsize=self.workers, max_retries=0
            )
            session.mount("https://", adapter)
            self._session = session
        return self._session

    def _detail(self, session, car_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = session.get(DETAIL_PAGE.format(car_id=car_id), timeout=self.timeout)
            if resp.status_code != 200:
                return None
            return detail_fields_from_html(decode(resp.content))
        except Exception:
            return None

    # ── teardown ──────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        if self._loop is None:
            return

        async def shutdown() -> None:
            await self._drop_browser()
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

        self._loop.run_until_complete(shutdown())
        self._loop.close()
        self._loop = None


async def _dismiss(dialog) -> None:
    """Best effort: by the time the alert fires the page may already be gone."""
    try:
        await dialog.dismiss()
    except Exception:
        pass
