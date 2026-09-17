"""
page_source.py -- Crawl Encar the way a browser does, not by calling the API.

Two steps, both through the Bright Data Scraping Browser:

  listing  load the real search page and read the XHR it fires for itself
  detail   fetch the car's detail page and pull the JSON embedded in its HTML

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
CloudFront blocks Bright Data's datacenter exit IPs unpredictably. When it
does, the page still loads 200 but fires no XHR at all and Encar throws a
JavaScript alert -- that is the signature to look for, not an HTTP error.
Across runs this worked sometimes and returned nothing other times, so the
listing step retries on a fresh CDP session, which is what changes the exit IP.
The detail step was not affected.

A residential proxy zone would remove the whole problem; see the README.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, parse_qs, unquote

SEARCH_PAGE = "https://www.encar.com/fc/fc_carsearchlist.do?carType=for"
DETAIL_PAGE = "https://fem.encar.com/cars/detail/{car_id}"

# The listing XHR, told apart from the promo carousels the page also loads by
# its `sr` parameter -- those ask for their own offsets and counts.
LISTING_PATH = "/search/car/list/"

# The detail HTML is one big JSON blob inside markup, so the fields are pulled
# out by name rather than by parsing a document that has no stable shape.
RE_VEHICLE_NO = re.compile(r'"vehicleNo"\s*:\s*"([^"]+)"')
RE_FIELD = {
    name: re.compile(r'"%s"\s*:\s*"([^"]*)"' % name)
    for name in ("manufacturerName", "modelName", "gradeName", "gradeDetailName")
}

# www.encar.com serves EUC-KR; fem.encar.com serves UTF-8.
ENCODINGS = ("utf-8", "euc-kr", "cp949")


def decode(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def search_url(action: str, page: int, limit: int,
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


def parse_detail(html: str, fallback: str) -> Tuple[Optional[str], str]:
    """Car number and model info out of a detail page's embedded JSON."""
    match = RE_VEHICLE_NO.search(html)
    car_number = match.group(1) if match else None

    parts: List[str] = []
    for name in ("manufacturerName", "modelName", "gradeName", "gradeDetailName"):
        found = RE_FIELD[name].search(html)
        if found and found.group(1):
            parts.append(found.group(1))

    return car_number, (" ".join(parts) or fallback)


class PageCrawler:
    """Drives one Scraping Browser session for both steps.

    The browser, the Playwright driver and the event loop are created once and
    kept: the CDP handshake costs about a second, which is more than the work
    it carries. `_reconnect()` throws the session away on purpose when the
    listing step is blocked, because a new session is what gets a new exit IP.
    """

    # When the listing XHR comes at all it lands within a few seconds of
    # navigation -- measured at 2.0s to 6.4s. Waiting a full minute for it only
    # makes a blocked attempt expensive, and being blocked is the common case.
    XHR_TIMEOUT = 20.0

    def __init__(self, ws_url: Optional[str] = None, workers: int = 8,
                 timeout: float = 60.0, log=print, browser: str = "local",
                 proxy_url: Optional[str] = None):
        if browser not in ("local", "brightdata"):
            raise ValueError(f"unknown browser: {browser!r}")
        if browser == "brightdata" and not ws_url:
            raise RuntimeError("browser=brightdata needs BRIGHTDATA_WS")

        self.ws_url = ws_url
        self.browser_kind = browser
        self.proxy_url = proxy_url
        self.workers = workers
        self.timeout = timeout
        self.log = log
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pw = None
        self._browser = None
        self._session = None
        self._proxy_lock = threading.Lock()

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
            if self.browser_kind == "brightdata":
                self._browser = await self._pw.chromium.connect_over_cdp(
                    self.ws_url, timeout=self.timeout * 1000
                )
            else:
                # Local Chromium, on this host's own IP. The listing step must
                # load www.encar.com/js/*, and a Bright Data residential zone
                # refuses those: robots.txt says `Disallow: /js/`, the zone
                # enforces it, and the site's JavaScript never boots. So the
                # local browser deliberately goes out unproxied.
                self._browser = await self._pw.chromium.launch()

        browser = self._browser
        if self.browser_kind == "brightdata":
            # The Scraping Browser hands out one pre-configured context and its
            # proxy lives there; a new_context() is not routed through it.
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
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

    def listing(self, action: str, page: int, limit: int, section: Optional[str],
                attempts: int = 3) -> Optional[Dict[str, Any]]:
        """The listing payload for one page, or None if every attempt was blocked."""
        for attempt in range(attempts):
            data = self._get_loop().run_until_complete(
                self._listing_once(action, page, limit, section)
            )
            if data is not None:
                return data
            if attempt < attempts - 1:
                self.log(f"    page {page}: no listing XHR "
                         f"(blocked), retrying on a fresh exit IP")
                self._get_loop().run_until_complete(self._drop_browser())
                time.sleep(2.0)
        return None

    async def _listing_once(self, action: str, page: int, limit: int,
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
            await pg.goto(search_url(action, page, limit), timeout=self.timeout * 1000,
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

    def details(self, items: List[Dict[str, Any]]) -> List[Tuple[Optional[str], str]]:
        """Car number and model info per listing, from the detail pages' HTML.

        This is plain HTTP on a thread pool, not the browser: the detail page is
        server-rendered and already carries the vehicle record as JSON in its
        markup, so there is nothing to execute. That also lets it go through the
        Bright Data residential proxy when one is configured -- `fem.encar.com`
        allows `/cars/detail/` paths, unlike `api.encar.com`.
        """
        if not items:
            return []

        import concurrent.futures

        def run(subset: List[Dict[str, Any]]):
            session = self._http()
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self.workers, len(subset))
            ) as pool:
                return list(pool.map(lambda it: self._one_detail(session, it), subset))

        had_proxy = bool(self.proxy_url)
        results = run(items)

        # If the proxy was dropped part-way through, the calls that went out
        # before that happened failed for a reason that no longer applies.
        if had_proxy and not self.proxy_url:
            retry_idx = [i for i, (no, _) in enumerate(results) if no is None]
            if retry_idx:
                self.log(f"    retrying {len(retry_idx)} detail page(s) direct")
                for i, res in zip(retry_idx, run([items[i] for i in retry_idx])):
                    results[i] = res

        return results

    def _http(self):
        if self._session is None:
            import requests
            import urllib3

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
            if self.proxy_url:
                session.proxies = {"http": self.proxy_url, "https": self.proxy_url}
                # Bright Data terminates TLS itself, so the chain is not Encar's.
                session.verify = False
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.workers, pool_maxsize=self.workers, max_retries=0
            )
            session.mount("https://", adapter)
            self._session = session
        return self._session

    def _one_detail(self, session, item: Dict[str, Any]) -> Tuple[Optional[str], str]:
        fallback = " ".join(
            str(p) for p in (item.get("Manufacturer"), item.get("Model"),
                             item.get("Badge")) if p
        )
        url = DETAIL_PAGE.format(car_id=item.get("Id"))
        try:
            resp = session.get(url, timeout=self.timeout)
            if resp.status_code == 402 and self._drop_proxy(resp):
                resp = session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                return None, fallback
            return parse_detail(decode(resp.content), fallback)
        except Exception:
            return None, fallback

    def _drop_proxy(self, resp) -> bool:
        """Stop using the proxy once Bright Data refuses the site outright.

        A residential zone without KYC enforces the target's robots.txt, and
        every Encar path this needs is disallowed there -- `fem.encar.com` bars
        `/cars/detail/`. That is a verdict about the account, not a transient
        error, so retrying through the proxy is pointless: drop it for the rest
        of the run and go direct, which works.
        """
        if not self.proxy_url:
            return False
        with self._proxy_lock:
            if not self.proxy_url:  # another thread got here first
                return True
            reason = "KYC" if b"kyc" in resp.content.lower() else "refused"
            self.log(f"    Bright Data proxy {reason} for the detail pages "
                     f"(HTTP 402) -- continuing direct")
            self.proxy_url = None
            if self._session is not None:
                self._session.proxies = {}
                self._session.verify = True
        return True

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
