# Encar crawling

    python main.py            # every page of the model, by whatever route works

[`main.py`](main.py) is the front door for exporting listings. The packages
below are the pieces it is built from, and each still runs on its own; they
share `.env`, `models.json` and the `output/` convention, and nothing else.

| Package | What it is for | Entry point |
|---|---|---|
| [`main.py`](main.py) | Export a model, trying the routes fastest-first and falling through when Encar blocks one | `python main.py` |
| [`listing_export/`](listing_export/) | One-off export of a model's listings: car number + model info, no database | `python listing_export/export_listings.py` |
| [`brightdata_collector/`](brightdata_collector/) | Run the Bright Data DCA collector and collect what it returns | `python brightdata_collector/trigger_collector.py` |
| [`stock_pipeline/`](stock_pipeline/) | The recurring crawl into MySQL, sold detection, and the dashboard | `python stock_pipeline/crawl_listings.py` |

## main.py

Every route reaches the same two facts per car — the vehicle number and the
model — at wildly different costs, and the cheap ones are the ones Encar blocks
first. So they are tried in order and the run stops at the first that delivers.
Measured on a 1130-car model:

| Route | One page of 20 | Notes |
|---|---|---|
| `api-direct` | **1.1s** | JSON, no rendering. Whole model in 21s. Free |
| `api-scrapingbee` | **11.9s** | Same JSON through ScrapingBee. Classic tier (1 credit) gets through; capped at ~3 concurrent by the plan |
| `api-brightdata` | — | Same again through Bright Data. **402 until the zone passes KYC** |
| `page-local` | 12.7s | Local Chromium for the listing, plain HTML for the detail |
| `collector` | 85s (231s for ten) | Hosted, renders everything, billed per run |

The two paid API routes exist for one situation: Encar has blocked this host's
IP. They are the only cheap way out of that, because they are still JSON — no
rendering, 0.9 KB a car — so they cost a fraction of what the collector does.

Two things keep a repeated export off the rate limit:

**The plate cache.** A vehicle number never changes, so a car id is looked up
once, ever ([`plate_cache.py`](listing_export/plate_cache.py)). The second run
over a model only asks about newly listed cars — 16000 cars drops from 16800
requests to 800, which is below the limit entirely. `--no-cache` to ignore it.

Because nothing in an entry ever changes, nothing can tell when one is wrong,
so entries are re-fetched after `--cache-max-age` days (90 by default). That
costs almost nothing: a listing rarely lives that long, so most entries are
retired by the car being delisted first.

**Automatic pacing.** Encar allows roughly 1200 requests before it starts
answering 403/407/429, and the block takes minutes to clear. A run that fits
inside that budget is not slowed down at all; one that does not paces itself to
hold about 4 requests/second. `--delay N` to override, `--delay 0` to run flat
out.

    --pages 1-5             page, range, or all (default)
    --model KEY             a models.json entry
    --strategies a,b,c      which routes to try, in order
    --race                  start the collector in the background when a route
                            fails, instead of waiting to reach it in turn
    --print                 dump the JSON to stdout as well as to output/

Check `complete` in the output before trusting a run: a listing page that never
landed takes its 20 cars with it, and the other counts are computed over the
rows that did arrive. `missingCars` and `pagesFailed` say what went missing.

### models.json

An entry names as much of the hierarchy as it wants, and the query stops at the
last level given — so the same file covers one model or a whole manufacturer.
Verified against the live API:

| Entry | `Count` |
|---|---|
| `bmw_3series_g20` — manufacturer + group + model | 1,127 |
| `bmw_3series` — manufacturer + group | 1,734 |
| `bmw` — manufacturer | 17,948 |
| `benz` | 16,932 |
| `hyundai` — `car_type: "Y"` for domestic makes | 50,382 |

## Tests

    python tests/test_crawl_logic.py

The decisions the crawl makes — when a run counts as complete, how hard to
brake, what the search expression looks like, what the cache may and may not
forget — with no network involved. Plain asserts, no pytest.

## stock_pipeline

    migrate.py             apply migrations/ (tracked in `schema_migrations`)
    crawl_listings.py      crawl -> `vehicles`, missing-streak sold detection
    sync_sold.py           crawl -> compare against `stock`, mark SOLD
    check_plate_status.py  ad-hoc "is this plate still on sale", no database
    import_rent_stock.py   load the sonokong product_group / rent_product dumps
    dashboard.py           local web UI (./serve.sh runs it behind gunicorn)

Shared modules, used by all of the above: `config.py` (paths + env settings),
`encar_api.py` (every Encar call), `db.py` (connection, run lock,
`crawl_batches`), `guards.py` (is this crawl healthy enough to conclude
anything from what is missing), `models.py`, `run_log.py`, `results.py`.

The daily crawl reuses the vehicle numbers it already stored: a car keeps its
number for as long as the listing exists, so only newly listed cars cost a
detail request. That is what keeps the crawl under Encar's request limit as
models are added — `--refresh-details` forces the old behaviour when a row
needs repairing.

## Output

Every tool writes the same document, so runs can be diffed across tools:

```json
{
  "tool": "listing_export",
  "name": "bmw_3series_g20",
  "crawledAt": "2026-09-17T11:39:14+07:00",
  "count": 5,
  "...": "run metadata",
  "rows": [{ "page": 1, "car_id": "42565728", "car_number": "…", "model_info": "…" }]
}
```

and the same flags, everywhere:

    --json PATH     write the run as JSON here
    --csv PATH      write the rows as CSV here (utf-8-sig, opens in Excel)
    --out-dir [DIR] write both, named <name>_<timestamp>, default
                    output/<tool>/ — so runs never overwrite each other

`output/` and `logs/` are not tracked in git, and neither is `var/` — which is
separate on purpose. `output/` holds results, safe to delete at any time;
`var/` holds what the next run reads: `plate_cache.json` (car numbers already
looked up) and `throttle_state.json` (which routes are still blocked). Deleting
the cache costs thousands of requests to rebuild.

## Setup

    pip install requests pymysql python-dotenv flask   # + playwright for --source page
    cp .env.example .env    # DB_*, BRIGHTDATA_*, API_TOKEN_BRINGDATA
    python stock_pipeline/migrate.py
