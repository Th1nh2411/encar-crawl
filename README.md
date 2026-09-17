# Encar crawling

Three packages, three jobs. They share `.env`, `models.json` and the `output/`
convention, and nothing else — each one runs on its own.

| Package | What it is for | Entry point |
|---|---|---|
| [`listing_export/`](listing_export/) | One-off export of a model's listings: car number + model info, no database | `python listing_export/export_listings.py` |
| [`brightdata_collector/`](brightdata_collector/) | Run the Bright Data DCA collector and collect what it returns | `python brightdata_collector/trigger_collector.py` |
| [`stock_pipeline/`](stock_pipeline/) | The recurring crawl into MySQL, sold detection, and the dashboard | `python stock_pipeline/crawl_listings.py` |

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

`output/` and `logs/` are not tracked in git.

## Setup

    pip install requests pymysql python-dotenv flask   # + playwright for --source page
    cp .env.example .env    # DB_*, BRIGHTDATA_*, API_TOKEN_BRINGDATA
    python stock_pipeline/migrate.py
