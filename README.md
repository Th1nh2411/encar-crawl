# Encar crawling

One crawler, one database. Every way of reading Encar — its JSON API from this
host or through a proxy service, a local browser, the hosted Bright Data
collector — reads the same listings and the same vehicle numbers. They are
routes to one result, not separate tools, so they share one row shape and land
in one SQLite file, `var/encar.db`. The dashboard, the exports and sold
detection all read that file.

    pip install -r requirements.txt
    python main.py dashboard                     # http://127.0.0.1:5050 — everything else is a button there

or from a shell:

    python main.py crawl --model bmw_3series_g20
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
    deploy/              PM2 config (+ optional Apache vhost) for a server
    scripts/             install-launchd.sh (Mac: keep the dashboard online)
    var/                 encar.db (+ crawl.lock) — the data; back it up
    output/  logs/       exports and the run log; safe to delete

`var/`, `output/` and `logs/` are not tracked in git.

## The dashboard

Everything is done from the web UI; nothing runs on a schedule.

| Action | Where | What it does |
|---|---|---|
| **Crawl now** | header, with a model picker | `main.py crawl` for that model, in the background; output shown live |
| **Import stock** | header | Reloads the sonokong stock from `data/` |
| **Export CSV / JSON** | Cars page | Downloads the cars view exactly as filtered |
| **Check plate** | Check plate tab | Live Encar lookup for up to 50 plates, next to the database's status |
| Cars / car page / Crawl runs | tabs | Status per plate, one car's ads and stock record, crawl history and log |

One background job at a time (crawl or import); a second is refused until it
finishes. The same commands still work from a shell (`python main.py …`).

Login: `DASHBOARD_USER` / `DASHBOARD_PASSWORD` in `.env` (HTTP Basic). With no
password the dashboard answers only direct local requests and refuses anything
that comes through a proxy (Apache, Cloudflare) — it cannot go online open.

## Server (PM2, http://ip:9191)

    sudo useradd -r -m -d /opt/encar-crawl encar
    sudo -u encar git clone https://github.com/Th1nh2411/encar-crawl.git /opt/encar-crawl
    cd /opt/encar-crawl
    sudo -u encar python3 -m venv .venv
    sudo -u encar .venv/bin/pip install -r requirements.txt
    # copy .env (with DASHBOARD_PASSWORD) and var/encar.db from the old machine;
    # chown them to encar. Without encar.db: run Import stock, then Crawl.

    # Can this server's IP reach Encar? 20 cars = yes; HTTP 407 = blocked, and
    # every crawl will fall through to the paid Bright Data route.
    sudo -u encar .venv/bin/python main.py crawl --model bmw_3series_g20 \
        --pages 1 --no-sold --routes api-direct

    # run it with PM2 (deploy/ecosystem.config.js), as the encar user
    sudo -u encar pm2 start deploy/ecosystem.config.js
    sudo -u encar pm2 save           # + `pm2 startup` once, to come back after a reboot
    sudo ufw allow 9191/tcp          # and the cloud security group, if any

Then open `http://<server-ip>:9191`. That is plain HTTP, so keep a
`DASHBOARD_PASSWORD` (the app refuses remote requests without one), and ideally
allow the port only from your own IPs. For HTTPS under a domain, put Apache in
front instead: `deploy/apache-encar.conf`, with the PM2 bind changed back to
`127.0.0.1:9191`.

No Cloudflare software is needed on a server; `serve.sh` and
`scripts/install-launchd.sh` are for running it from a Mac. Run it in one
place only: two machines means two databases that drift apart.

## From a Mac (Cloudflare tunnel)

    ./serve.sh --local                # gunicorn on http://127.0.0.1:5050 only
    ./serve.sh                        # + Cloudflare tunnel, public HTTPS URL
    scripts/install-launchd.sh        # the same, always on (restarts itself)
    scripts/install-launchd.sh --uninstall

`serve.sh` will not open a tunnel without `DASHBOARD_PASSWORD`. Without
`CLOUDFLARE_TUNNEL_TOKEN` the URL is a random `*.trycloudflare.com` one that
changes on every restart (current one: `logs/tunnel-url.txt`); with a named
tunnel from the Cloudflare dashboard, routed to `http://localhost:5050`, it is
fixed. The site is up only while the Mac is awake.

## Tests

    python tests/test_crawl_logic.py

The decisions — when a route counts, when sold detection may run, how rows
merge, what the cache may forget — with no network. Plain asserts, no pytest.
