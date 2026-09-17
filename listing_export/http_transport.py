"""
http_transport.py -- How listing_export reaches Encar.

Three backends behind one `get_many(requests)` batch call:

  direct      plain HTTPS from this host, on a thread pool. Verified to work
              with no headers at all, so this is the fast path and the default.
  bd-proxy    the same, routed through a Bright Data *proxy* zone.
  bd-browser  Bright Data's Scraping Browser, driven over CDP.

Which Bright Data backend applies depends on the zone, and the zone decides for
itself: pointing a Scraping Browser credential at the proxy port is answered
with `403 You are trying to use Scraping Browser zone as regular proxy`. So the
rule is explicit rather than guessed -- BRIGHTDATA_PROXY_URL selects the proxy,
BRIGHTDATA_WS selects the browser.

`auto` (the default) goes direct and only falls back to Bright Data once Encar
has actually throttled the run. Both Bright Data paths are slower than direct
by a wide margin -- the browser one boots a real Chrome -- so they earn their
place as the thing that keeps a run alive, not as the way it normally goes.

The batch shape matters: the browser backend pays a fixed connection cost, and
handing it all 20 detail requests at once means paying it once.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, quote, urlencode

import requests

# Encar sits behind CloudFront. These are the statuses it answers a client with
# when it is rate limiting rather than genuinely refusing the request.
THROTTLE_STATUSES = (403, 407, 429)

# One request: a URL and its query parameters.
Req = Tuple[str, Optional[Dict[str, Any]]]

# Sent because Encar's own front-end sends them, not because they are required:
# a request with no headers at all also returns 200. Keeping them costs nothing
# and makes the traffic look like the site's own.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.encar.com/fc/fc_carsearchlist.do?carType=for",
    "Origin": "https://www.encar.com",
}


@dataclass
class Result:
    """One response, reduced to what the caller acts on."""

    data: Optional[Dict[str, Any]] = None
    status: Optional[int] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.data is not None

    @property
    def gone(self) -> bool:
        """404 -- the listing was pulled between the list and the detail call."""
        return self.status == 404

    @property
    def throttled(self) -> bool:
        return self.status in THROTTLE_STATUSES


def full_url(url: str, params: Optional[Dict[str, Any]]) -> str:
    return f"{url}?{urlencode(params)}" if params else url


# ── Direct ────────────────────────────────────────────────────────────────────

class _BlockCookies:
    """Cookie policy that stores nothing.

    A `requests.Session` is only awkward to share between threads because of
    its cookie jar. Encar needs no cookies -- the API answers an anonymous
    request -- so refusing them outright makes one Session safe for all the
    workers and saves a round of Set-Cookie parsing per response.
    """

    netscape = True
    rfc2965 = False
    hide_cookie2 = False

    def set_ok(self, *_a, **_kw) -> bool:
        return False

    def return_ok(self, *_a, **_kw) -> bool:
        return False

    def domain_return_ok(self, *_a, **_kw) -> bool:
        return False

    def path_return_ok(self, *_a, **_kw) -> bool:
        return False


class DirectTransport:
    """Pooled, cookie-less Session over a thread pool. Optionally proxied."""

    name = "direct"

    def __init__(self, workers: int = 8, timeout: float = 15.0,
                 proxies: Optional[dict] = None):
        self.workers = workers
        self.timeout = timeout
        self.proxies = proxies

        session = requests.Session()
        session.headers.update(HEADERS)
        session.cookies.set_policy(_BlockCookies())
        # macOS system proxy settings leak in through trust_env and answer every
        # Encar call with HTTP 407, which looks exactly like being rate limited.
        session.trust_env = False
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=workers, pool_maxsize=workers, max_retries=0
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        self.session = session

    def _one(self, req: Req) -> Result:
        url, params = req
        try:
            resp = self.session.get(
                url, params=params, timeout=self.timeout, proxies=self.proxies,
                # Bright Data terminates TLS itself, so the chain seen on that
                # path is not Encar's. Checking stays on when going direct.
                verify=self.proxies is None,
            )
        except requests.RequestException as exc:
            return Result(error=f"{type(exc).__name__}: {exc}")

        if resp.status_code == 200:
            try:
                return Result(data=resp.json() if resp.content else None,
                              status=200)
            except ValueError as exc:
                return Result(status=200, error=f"bad JSON: {exc}")
        return Result(status=resp.status_code, error=f"HTTP {resp.status_code}")

    def get_many(self, reqs: Sequence[Req]) -> List[Result]:
        if not reqs:
            return []
        if len(reqs) == 1:
            return [self._one(reqs[0])]

        # map() keeps the result order, which is the page order the caller
        # asked for; the pool is small enough that head-of-line waiting is free.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.workers, len(reqs))
        ) as pool:
            return list(pool.map(self._one, reqs))

    def close(self) -> None:
        self.session.close()


class BrightDataProxyTransport(DirectTransport):
    name = "bd-proxy"

    def __init__(self, proxy_url: str, workers: int = 8, timeout: float = 30.0):
        super().__init__(workers=workers, timeout=timeout,
                         proxies={"http": proxy_url, "https": proxy_url})


# ── Bright Data Scraping Browser ──────────────────────────────────────────────

class BrightDataBrowserTransport:
    """Fetches through Bright Data's Scraping Browser over CDP.

    The browser is only a transport here: these are JSON endpoints, so nothing
    is rendered. Playwright's APIRequestContext issues the requests from inside
    the remote browser, which keeps Bright Data's exit IP and TLS fingerprint
    without paying for a page load.

    The async API is what makes this worth using at all -- the sync one binds
    its objects to one thread, which would force 21 requests to run one after
    another. Here they are gathered on a single connection.

    Measured against this zone (8 detail calls):

        CDP connect    1.02s     <- fixed, per connection
        1 request      0.59s
        8 concurrent   0.59s     <- concurrency is effectively free
        8 sequential   2.42s

    The handshake costs more than the requests do, so the connection is opened
    once and held for the whole run. `reconnect()` is the deliberate exception:
    CloudFront throttles Bright Data exit IPs one at a time, and a new CDP
    session is what gets a different one.

    The event loop is created once and kept, because Playwright's objects are
    bound to the loop that made them -- `asyncio.run()` per batch would close
    the loop and take the browser with it.
    """

    name = "bd-browser"

    def __init__(self, ws_url: str, workers: int = 8, timeout: float = 60.0):
        self.ws_url = ws_url
        self.workers = workers
        self.timeout = timeout
        self._loop = None
        self._pw = None
        self._browser = None

    # ── connection ────────────────────────────────────────────────────────

    def _get_loop(self):
        import asyncio
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop

    async def _connect(self) -> None:
        from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(
            self.ws_url, timeout=self.timeout * 1000
        )

    async def _disconnect_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # the session is going away regardless
                pass
            self._browser = None

    def reconnect(self) -> None:
        """Drop the CDP session so the next batch goes out on a fresh exit IP."""
        if self._browser is None:
            return
        self._get_loop().run_until_complete(self._disconnect_browser())

    # ── fetching ──────────────────────────────────────────────────────────

    def get_many(self, reqs: Sequence[Req]) -> List[Result]:
        if not reqs:
            return []
        try:
            import playwright  # noqa: F401
        except ImportError:
            return [Result(error="playwright is not installed "
                                 "(pip install playwright)")] * len(reqs)
        return self._get_loop().run_until_complete(self._run(reqs))

    async def _run(self, reqs: Sequence[Req]) -> List[Result]:
        import asyncio

        if self._browser is None:
            try:
                await self._connect()
            except Exception as exc:  # the CDP handshake fails as a unit
                return [Result(error=f"Bright Data connect failed: {exc}")] * len(reqs)

        # The Scraping Browser hands out one pre-configured context and its
        # proxy lives there. A new_context() is not routed through it and every
        # request comes back 407, so reuse what is already there.
        browser = self._browser
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        sem = asyncio.Semaphore(self.workers)

        async def one(req: Req) -> Result:
            url, params = req
            async with sem:
                try:
                    resp = await ctx.request.get(
                        full_url(url, params),
                        headers=HEADERS,
                        timeout=self.timeout * 1000,
                    )
                except Exception as exc:
                    return Result(error=f"{type(exc).__name__}: {exc}")
                if resp.status == 200:
                    try:
                        return Result(data=await resp.json(), status=200)
                    except Exception as exc:
                        return Result(status=200, error=f"bad JSON: {exc}")
                return Result(status=resp.status, error=f"HTTP {resp.status}")

        return list(await asyncio.gather(*(one(r) for r in reqs)))

    def close(self) -> None:
        if self._loop is None:
            return

        async def shutdown() -> None:
            await self._disconnect_browser()
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

        self._loop.run_until_complete(shutdown())
        self._loop.close()
        self._loop = None


# ── Selection ─────────────────────────────────────────────────────────────────

def brightdata_proxy_url() -> Optional[str]:
    """Proxy URL for a Bright Data *proxy* zone, if one is configured.

    BRIGHTDATA_PROXY_URL is taken as given. Otherwise a proxy zone can be
    described the same way as the browser one -- credentials in a URL -- and
    BRIGHTDATA_PROXY_HOST/PORT say where to send them.
    """
    explicit = (os.getenv("BRIGHTDATA_PROXY_URL") or "").strip()
    if explicit:
        return explicit

    host = (os.getenv("BRIGHTDATA_PROXY_HOST") or "").strip()
    if not host:
        return None

    parts = urlsplit((os.getenv("BRIGHTDATA_WS") or "").strip())
    if not parts.username or not parts.password:
        return None

    port = (os.getenv("BRIGHTDATA_PROXY_PORT") or "33335").strip()
    user = quote(parts.username, safe="")
    password = quote(parts.password, safe="")
    return f"http://{user}:{password}@{host}:{port}"


def brightdata_ws_url() -> Optional[str]:
    ws = (os.getenv("BRIGHTDATA_WS") or "").strip()
    return ws or None


def brightdata_available() -> bool:
    return brightdata_proxy_url() is not None or brightdata_ws_url() is not None


def make_transport(kind: str, workers: int = 8):
    """`kind` is one of direct / brightdata."""
    if kind == "direct":
        return DirectTransport(workers=workers)

    if kind == "brightdata":
        proxy = brightdata_proxy_url()
        if proxy:
            return BrightDataProxyTransport(proxy, workers=workers)
        ws = brightdata_ws_url()
        if ws:
            return BrightDataBrowserTransport(ws, workers=workers)
        raise RuntimeError(
            "Bright Data is not configured: set BRIGHTDATA_WS (Scraping "
            "Browser) or BRIGHTDATA_PROXY_URL (proxy zone) in .env."
        )

    raise ValueError(f"unknown transport: {kind!r}")
