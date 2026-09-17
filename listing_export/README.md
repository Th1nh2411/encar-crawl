# listing_export

First page of one Encar model, two fields per car: **car number** and **model info**.

Standalone — no database, no migrations, no browser on the normal path. It shares
`.env` and `models.json` with the project root, and touches nothing else.

## Run

```bash
python listing_export/export_listings.py                     # 일반등록, page 1, 20 cars
python listing_export/export_listings.py --pages 3
python listing_export/export_listings.py --pages 2-5 --csv out.csv
python listing_export/export_listings.py --pages all --out-dir
python listing_export/export_listings.py --source api --transport direct
```

`--pages` takes `1`, `2-5` or `all`, 1-based like the site. `--section general`
is 일반등록 and the default; `premium` is the paid-placement tab. Both cover the
same cars — the Count is identical — and differ only in ordering.

## Why the listing browser is local, not Bright Data

Because Bright Data cannot load Encar's JavaScript, and without it the search
page never runs a search. Both Bright Data options were tested against the
real site:

| Path | Result |
|---|---|
| Residential zone (no KYC) | **402** on every Encar path this needs. The zone enforces the target's `robots.txt`, and `www.encar.com` says `Disallow: /js/` — so jQuery, require.js and the app bundle are all refused (`net::ERR_ABORTED`, then `jQuery is not defined`). `api.encar.com` says `Disallow: /`, and `fem.encar.com` bars `/cars/detail/`. |
| Scraping Browser zone (datacenter IPs) | Loads the JavaScript fine — it does not enforce robots.txt — but Encar serves its exit IPs nothing. It worked once, then stopped. |
| Local Chromium, unproxied | **Works.** 5 `api.encar.com` XHRs, all 200, 20 rows. |

So the listing runs in a local browser on this host's own IP. The detail step
still tries the residential proxy when `BRIGHTDATA_PROXY_URL` is set, and drops
it to direct on the first 402 — that refusal is about the account, not the
request, so it never retries through the proxy afterwards.

Passing KYC at <https://brightdata.com/cp/kyc> is what would lift the robots
enforcement and make `--browser brightdata` usable.

### The hash has to be spelled exactly right

The search page's state lives in its `#!{...}` fragment, and its router only
recognises the site's own spelling: **no separator spaces**, and the query's
parentheses left literal. `json.dumps` defaults to `", "` and `": "`, which is
enough for the page to load normally and never run a search — no error, no
XHR, just an empty result area. That one detail accounted for every "blocked"
run during development.

## Two sources

| `--source` | Listing from | Car number from | State |
|---|---|---|---|
| `page` (default) | the search page's own XHR, rendered in a local browser | the detail page's HTML | **works** — 20/20 in 12s, 59/60 over three pages in 22s |
| `api` | `api.encar.com/search/car/list/...` | `api.encar.com/v1/readside/...` | works, faster, but builds the API call itself |

`--source page` calls no Encar API directly. The search URL keeps its state in
a hash fragment (`#!{...,"page":2,...}`), and a hash is never sent to the
server — fetching that URL over HTTP returns the same 227 KB app shell every
time, with `carid` appearing zero times in it. Only the page's own JavaScript
turns that hash into a request, so the listing needs a real browser.

Measured on the page source:

- **Listing**: the local browser loads the search page, which fires its own
  `/search/car/list/general` request; that response is intercepted and used.
  20 cars per page, several pages in one browser session.
- **Detail**: plain HTTP, no browser. The detail page is server-rendered and
  carries the whole vehicle record as JSON in its markup, `vehicleNo` included,
  so it is fetched and parsed directly — 5 cars in 1.94s.

JSON goes to stdout, progress to stderr, so `> rows.json` gives a clean file.

```json
{
  "model": "bmw_3series_g20",
  "count": 20,
  "transport": "direct",
  "elapsed_seconds": 1.3,
  "rows": [
    { "car_id": "40978119", "car_number": "300주5081",
      "model_info": "BMW 3시리즈 (G20) M340i xDrive 투어링" }
  ]
}
```

`car_id` is Encar's listing id. It is not one of the two requested fields, but
without it a row cannot be traced back to a listing, and it costs nothing.

## Requests

```
GET api.encar.com/search/car/list/premium?q=<model>&sr=|ModifiedDate|0|20
GET api.encar.com/v1/readside/vehicle/<id>?include=CATEGORY          × 20
```

21 requests for 20 cars. The detail call cannot be avoided: the listing
endpoint does not return the vehicle number — only the per-vehicle endpoint
does — and that is the field being asked for.

## Why it is fast

Measured, 20 rows: **~1.3s** direct.

| Choice | Effect |
|---|---|
| `include=CATEGORY` on detail | 11 KB → 0.9 KB per response, **12× less** to transfer and parse, on the 20 requests that dominate the run |
| One batch on an 8-thread pool | 20 detail calls overlap instead of queueing |
| Pooled, cookie-less Session | TLS handshakes reused; no `Set-Cookie` parsing, and no cookie jar to make sharing threads unsafe |
| No cookie warm-up | `stock_pipeline` opens `encar.com` first to look like a browser. The API answers a request with *no headers at all*, so that round trip buys nothing |
| `trust_env = False` | macOS system proxy settings otherwise leak in and answer every call with 407 |

## Transports

`--transport auto` (default) goes direct and falls back to Bright Data only
once Encar has actually throttled the run.

| Mode | What it uses |
|---|---|
| `direct` | plain HTTPS from this host — fast path |
| `brightdata` | `BRIGHTDATA_PROXY_URL` if set, otherwise the Scraping Browser at `BRIGHTDATA_WS` |
| `auto` | direct, escalating to Bright Data on 403/407/429 |

### Bright Data, as measured

The credential in `.env` is a **Scraping Browser** zone, reached over CDP.
Measured against it, 8 detail calls:

```
CDP connect    1.02s     ← fixed, per connection
1 request      0.59s
8 concurrent   0.59s     ← concurrency is effectively free
8 sequential   2.42s
```

The handshake costs more than the requests, so the connection is opened once
and held for the whole run: a second batch on a live connection takes **0.23s**
against 2.21s for the first one including connect.

Four things were verified the hard way:

- **The proxy port does not work with this zone.** `brd.superproxy.io:33335`
  answers `403 You are trying to use Scraping Browser zone as regular proxy`.
  Set `BRIGHTDATA_PROXY_URL` only if you add a real proxy zone.
- **Reuse `browser.contexts[0]`.** A `new_context()` is not routed through the
  zone's proxy and every request comes back 407.
- **Playwright objects are bound to the loop that created them**, so the event
  loop is created once and kept. `asyncio.run()` per batch would close the loop
  and take the browser with it.
- **The search endpoint is unreliable from datacenter IPs.** CloudFront
  throttles Bright Data exit IPs one at a time. Success on the listing call
  ranged from 2 runs in 3 down to 1 in 4 as testing continued, and it degrades
  the harder you push. `reconnect()` drops the CDP session to land on a
  different exit IP, which is why a throttle retries rather than waits. The
  detail endpoint was never affected.

### Scraping Browser is the wrong Bright Data product here

It boots a real Chrome to read a JSON document. Encar's API needs no rendering
and throws no captcha, so everything the browser provides is waste — including
that 1s handshake. Either of these would fit better:

- **A residential proxy zone.** `DirectTransport` already takes `proxies`, so
  setting `BRIGHTDATA_PROXY_URL` uses it with no browser and no Playwright at
  all. Residential IPs are also what the search endpoint's 407 is asking for.
- **Web Unlocker**, one HTTP call per URL against `api.brightdata.com`.

Both need a working `API_TOKEN_BRINGDATA` to set up; the one in `.env` returns
`401 Token expired`.

As it stands Bright Data is **3–8× slower** than direct and costs per request.
It is insurance, not the normal route.

## Requirements

`requests` for the direct path. `playwright` only if you use the Scraping
Browser. `python-dotenv` is optional — without it, export the vars yourself.
