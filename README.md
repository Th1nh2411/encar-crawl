# Encar crawling

One crawler, one database. Every way of reading Encar — its JSON API from this
host or through a proxy service, a local browser, the hosted Bright Data
collector — reads the same listings and the same vehicle numbers. They are
routes to one result, not separate tools, so they share one row shape and land
in one SQLite file, `var/encar.db`. The dashboard, the exports and sold
detection all read that file.

    python main.py crawl                         # every model, every page
    python main.py crawl --model bmw_3series_g20
    python main.py dashboard                     # http://127.0.0.1:5050
    python main.py export --view cars            # JSON + CSV into output/cars/
    python main.py check-plate 110모5877          # live lookup by plate
    python main.py import-stock                  # sonokong stock from data/

## How a crawl runs

For each model in `models.json`:

1. **Open a batch** in `crawl_batches`.
2. **Load the car numbers already known** from `vehicles`. A listing keeps its
   vehicle number for as long as it exists, so those cars cost no request.
   Numbers older than `--detail-max-age` (90 days) are looked up again.
3. **Walk the route ladder**, fastest first, stopping at the first route
   that delivers (see below).
4. **Store every row** into `vehicles`, merging: a sparse route never blanks
   a field a richer one stored.
5. **Sold detection**, only if the run passes the guards in
   [`guards.py`](encar/guards.py): every page covered, ≥99% of pages and cars
   arrived, not a collapse against the last good run. Listings of the model
   that were not seen get `missing_streak + 1`; at 2 consecutive misses they
   become `SOLD`. A listing seen again goes straight back to live.
6. **Close the batch** as OK / PARTIAL / FAILED, with the route used and what
   every route did.

One crawl at a time (lock file `var/crawl.lock`); a second exits with code 2.

## The route ladder

| # | Route | How | One page of 20 | Cost / caveat |
|---|---|---|---|---|
| 1 | `api-direct` | `api.encar.com` JSON from this host | 1.1s | Free. Encar answers 403/407/429 once a host asks too often |
| 2 | `api-scrapingbee` | Same JSON through ScrapingBee | 11.9s | Credits; ~3 concurrent on the plan. Needs `SCRAPINGBEE_KEY` |
| 3 | `api-brightdata` | Same JSON through a Bright Data zone | ~15s | Needs `BRIGHTDATA_PROXY_URL` or `BRIGHTDATA_WS`. A zone without KYC answers 402 |
| 4 | `page-local` | Local Chromium reads the search page's own XHR; plate from the detail page HTML | 12.7s | Free, same IP as route 1. **Currently blocked** (see below) |
| 5 | `collector` | Hosted Bright Data collector renders every page | 85s | Billed per run; capped at 10 pages; never drives sold detection. Needs `API_TOKEN_BRINGDATA` |

A route is dropped when it raises, returns no rows, fills under 80% of the
car numbers (`--min-fill`), or loses more than 2% of the cars Encar's Count
says exist. The fullest dropped run is still used if nothing better lands,
marked incomplete — and an incomplete run never marks anything sold.

Blocks are remembered in the `route_state` table: a throttled route is skipped
for 5 minutes (`--cooldown`), an account refusal (402, quota spent) for a day.
`--race` starts the collector in the background as soon as a route fails.

Within a route, throttled requests are retried (3 rounds, 10s apart) except on
the very first request, where a block means "move to the next route now".
Pacing is automatic: nothing under Encar's ~1200-request budget, ~4 req/s
above it (`--delay` to override).

`--pages 1-5` (the site's pages of 20) crawls part of a model — useful for a
quick look, but it never runs sold detection.

`page-local` stopped working before this refactor: the search page loads but
never fires its listing XHR in headless Chromium, with the old code too. It
stays in the ladder; the routes above it carry the crawl.

## The database

| Table / view | What |
|---|---|
| `vehicles` | One row per Encar listing (`car_id`): plate, model, price, mileage, colour…, `status` ACTIVE/SOLD, `missing_streak`, first/last seen |
| `crawl_batches` | One row per model per crawl: route, attempts, coverage, sold check |
| `route_state` | Routes on hold and until when |
| `product_group`, `rent_product` | The sonokong stock, from `import-stock` |
| **`car_status`** (view) | **The one to read.** One row per plate, Encar and stock joined |

`car_status.status`:

| Status | | Meaning |
|---|---|---|
| `ON_SALE` | 판매중 | At least one ad is live and was seen in the latest crawl |
| `CHECKING` | 확인중 | Missing from the latest crawl; one more miss and it is sold |
| `SOLD` | 판매완료 | Every ad for the plate is gone |
| `NOT_FOUND` | 미확인 | In stock but never seen on Encar — its model is not in `models.json` |

A car is often posted by several dealers (several `car_id`, one plate), so
status is per plate: sold only once every ad is gone.

Query it directly with `sqlite3 var/encar.db "select status, count(*) from car_status group by 1"`.

## models.json

An entry names as much of the hierarchy as it wants; the query stops at the
last level given:

| Entry | Listings |
|---|---|
| `bmw_3series_g20` — manufacturer + group + model | ~1,100 |
| `bmw_3series` — manufacturer + group | ~1,700 |
| `bmw` — manufacturer | ~18,000 |
| `hyundai` — `car_type: "Y"` for domestic makes | ~50,000 |

The first crawl of a big model is one detail request per car (≈4 req/s once
paced, so `hyundai` is hours); every crawl after that only asks about new
cars. Overlapping entries (`bmw` and `bmw_3series_g20`) are safe: a listing
belongs to the model whose crawl last saw it, and only that model's complete
crawls can mark it missing.

To resolve the stock's 미확인 cars, add their models (Korean makes need
`"car_type": "Y"`) and crawl.

## Files

    main.py              entry point -> encar/cli.py
    encar/               the package
      routes.py ladder.py   the five routes and the fallback ladder
      api.py transport.py   JSON API: listing + detail, pacing, transports
      browser.py collector.py   the browser route, the hosted collector
      parse.py             the shared row shape
      store.py guards.py crawl.py   database, sold-detection gate, orchestration
      dashboard.py templates/   the web UI
      plates.py stock_import.py export.py
    collector/           the Bright Data collector's own scripts (run in its IDE)
    data/                sonokong dumps, sample plate list
    var/                 encar.db (+ crawl.lock) — the data; back it up
    output/  logs/       exports and the run log; safe to delete

`var/`, `output/` and `logs/` are not tracked in git.

## Setup

    pip install requests python-dotenv flask   # + playwright for page-local
    cp .env.example .env                        # proxy / collector credentials
    python main.py import-stock
    python main.py crawl --model bmw_3series_g20

## Online

    ./serve.sh --local                # gunicorn on http://127.0.0.1:5050 only
    ./serve.sh                        # + Cloudflare tunnel, public HTTPS URL
    scripts/install-launchd.sh        # both, always on, plus a daily crawl
    scripts/install-launchd.sh --models "bmw_3series_g20 benz" --at 05:30
    scripts/install-launchd.sh --uninstall

The dashboard asks for a login (`DASHBOARD_USER` / `DASHBOARD_PASSWORD` in
`.env`); `serve.sh` will not open a tunnel without a password, and without one
the app refuses anything that arrives through Cloudflare.

Without `CLOUDFLARE_TUNNEL_TOKEN` the tunnel is a quick one: a random
`*.trycloudflare.com` URL that changes whenever it restarts — the current one
is in `logs/tunnel-url.txt`. For a fixed hostname, create a tunnel in the
Cloudflare dashboard (Zero Trust → Networks → Tunnels), route its public
hostname to `http://localhost:5050`, and put its token in `.env`.

The site is up only while this Mac is awake: the LaunchAgent runs under
`caffeinate -is`, but a closed lid still sleeps a laptop. Crawls run from the
Mac's own IP, which is what Encar lets through.

## Tests

    python tests/test_crawl_logic.py

The decisions — when a route counts, when sold detection may run, how rows
merge, what the cache may forget — with no network. Plain asserts, no pytest.
