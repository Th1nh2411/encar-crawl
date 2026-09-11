# Plan: Encar Model-Specific Crawling & Stock "Sold" Detection

## 1. Purpose

Crawl listings for a specific model from Encar and compare the results against our internal `stock` table.
Any vehicle number that exists in our `stock` table but no longer appears in the crawled data should be **marked as sold**.

The previous approach crawled full listing detail pages, which made the process too slow.
For this plan, the crawl target is much smaller (only 2 fields), so **the top priority is minimizing crawl time** through a lightweight approach.

---

## 2. Scope

### API verification result (done before development — supersedes the original assumption)

The original plan assumed the vehicle number could be read straight off the listing page. It cannot. Verified against the live API on 2026-09-10:

| Check | Result |
|---|---|
| Does the listing API return the vehicle number? | **No.** Neither `/search/car/list/premium` nor `/search/car/list/general` returns `vehicleNo`. The listing payload only carries `Id`, `Manufacturer`, `Model`, `Badge`, `Year`, `Mileage`, `Price`, `ServiceCopyCar`, dealer fields, photos. |
| Where does the vehicle number live? | Only on the per-vehicle detail endpoint `/v1/readside/vehicle/<carId>` (~0.47s/request sequential). |
| Max listing page size | ≥ 200 rows per request — 1,181 G20 listings fit in 6 requests. |

Three options followed from this:

| Option | Requests per model | Note |
|---|---|---|
| A — listing + detail per car | ~1,187 | Restores the vehicle number, but reintroduces the detail-page cost the plan set out to remove |
| B — one keyword search per plate in `stock` | 1 per stock vehicle | The existing `status_crawl.py` approach |
| **C — listing only, match on Encar `carId`** | **~6** | **Chosen.** Requires `stock` to store the Encar listing id — confirmed it does |

**Decision: option C.** The matching key is the Encar listing id (`car_id`), not the vehicle number.

### Data to crawl (from the listing API only)

| Field | Description |
|---|---|
| `Id` → `car_id` (차량번호 대체 키) | **Matching key** — the Encar listing id |
| Model info (모델정보) | `Manufacturer + Model + Badge` — model name level detail |
| `ServiceCopyCar` → `is_duplicate` | Marks dealer duplicate listings; kept so a duplicate is never mistaken for a separate car |

> Price, options, photos, dealer info, etc. from detail pages are **not** collected. `vehicle_no` stays as a nullable column for an optional later enrichment pass, but nothing in the comparison depends on it.

### Process flow

1. Crawl Encar's search API filtered to a specific model, 200 rows per request → extract `car_id` + model info
2. Store the crawled results in a separate table (`crawled_vehicles`)
3. Compare `crawled_vehicles` against our `stock` table, matched by `car_id`
4. Any `car_id` present in `stock` (scoped to the crawled model) but absent from `crawled_vehicles` → **update status to SOLD**

> **Scoping matters**: a batch only crawls one model, so the comparison must be restricted to the `stock` rows of that model. Each entry in `models.json` carries a `stock_filter` SQL fragment for exactly this. Without it, every other model in `stock` would look "missing" and be sold off.

### Out of scope

- Collecting listing detail data (price, options, photos, etc.)
- Root-cause analysis of why a listing disappeared (fake listing vs. actual sale)
- Integration with platforms other than Encar

---

## 3. Data Model (draft)

### `crawled_vehicles` (snapshot table, refreshed on every crawl cycle)

| Column | Type | Description |
|---|---|---|
| id | PK | |
| crawl_batch_id | bigint, FK | Identifier for this crawl run (used in the comparison logic) |
| car_id | varchar, index | **Encar listing id — the matching key** |
| vehicle_no | varchar, index, nullable | Not available from the listing API; reserved for optional enrichment |
| model_info | varchar | Manufacturer + Model + Badge |
| is_duplicate | bool | `ServiceCopyCar = DUPLICATION` |
| crawled_at | datetime | |

> This table should be treated as a **full snapshot on every run** rather than an accumulating log. Either truncate and reload the table each cycle, or filter by the latest `crawl_batch_id` when comparing — this keeps "listing disappeared" logic simple.

### `crawl_batches` (one row per crawl run — holds the safeguard metrics)

| Column | Type | Description |
|---|---|---|
| id / batch_id | PK | Referenced by `crawled_vehicles.crawl_batch_id` |
| started_at / finished_at | datetime | |
| total_expected_pages | int | From the total result count of the first response |
| fetched_pages | int | Pages fetched successfully |
| completion_rate | decimal | `fetched_pages / total_expected_pages` |
| crawled_count | int | Rows written for this batch |
| status | enum | `RUNNING` / `OK` / `PARTIAL` / `FAILED` |
| comparison_ran | bool | Whether the SOLD comparison was executed |

> The comparison job reads this table to decide whether the batch qualifies (see §4). Keeping the metrics here also gives the previous-batch `crawled_count` needed by the drop guard.

### `stock` (existing inventory table)

- Keep the existing structure; join/compare on the **Encar listing id** column
- Assumes a status field already exists to represent "sold"
- This repo does not own that schema, so the table and column names are read from env (`STOCK_TABLE`, `STOCK_CAR_ID_COL`, `STOCK_STATUS_COL`, `STOCK_SOLD_AT_COL`, `STOCK_SOLD_VALUE`)

---

## 4. Comparison (matching) logic

```
for each car_id in stock (status != SOLD AND matches this batch's model):
    if car_id not in crawled_vehicles(this batch):
        stock.status = SOLD
        stock.sold_confirmed_at = now()
```

Implemented as a single LEFT JOIN update:

```sql
UPDATE stock s
LEFT JOIN crawled_vehicles cv
       ON cv.car_id = s.car_id AND cv.crawl_batch_id = :batch_id
SET s.status = 'SOLD', s.sold_confirmed_at = NOW()
WHERE <stock_filter for this model>
  AND s.status <> 'SOLD'
  AND s.car_id IS NOT NULL AND s.car_id <> ''
  AND cv.id IS NULL;
```

- **Caution**: if the crawl partially fails (blocking, network errors, incomplete pagination), valid still-listed vehicles could be incorrectly marked as "not found"

### Safeguard: crawl completion-rate threshold (decided)

The comparison logic only runs when the crawl run is considered complete enough. Completion rate is measured per run:

```
completion_rate = successfully fetched pages / total expected pages
```

`total_expected_pages` comes from the total result count returned by the first listing/API response (`ceil(total_count / page_size)`), captured at the start of the run.

| Completion rate | Action |
|---|---|
| ≥ 99% | Run comparison, mark missing vehicles as SOLD |
| 95% – 99% | Skip comparison, mark batch as `PARTIAL`, alert |
| < 95% | Abort run, mark batch as `FAILED`, alert |

Additional guards on the same run:

- **Empty/near-empty result guard**: if the crawled row count for the target model drops more than 50% versus the previous successful batch, skip the comparison and alert — this catches silent blocking or a changed page structure that still returns HTTP 200.
- **Zero-row guard**: a batch with 0 crawled rows never triggers the comparison, regardless of completion rate.
- A skipped/aborted run changes no `stock` rows; the next successful run performs the comparison.

Thresholds are stored as config (not hardcoded) so they can be tuned after observing real run statistics.

---

## 5. Crawl optimization (key requirement)

The old approach was slow because it visited **individual detail pages** to pull large amounts of data. What the implementation actually does:

1. **Listing API only — no detail pages, no browser, no Bright Data**
   `GET https://api.encar.com/search/car/list/premium?count=true&q=<model filter>&sr=|ModifiedDate|<offset>|<limit>`, parsed as JSON. Bright Data's Scraping Browser is not needed at all for this job, which removes both its latency and its cost.

2. **200 rows per request**
   Tested up to 200 (the UI uses far less). 1,181 G20 listings = 6 requests instead of 79 at the default page size.

3. **Scope the crawl to a specific model filter**
   One entry per model in `models.json`; the search query is built from `manufacturer` / `model_group` / `model`.

4. **Parallel page fetching**
   4 workers by default (`SYNC_LIST_WORKERS`), each with its own keep-alive `Session`. Reusing a Session — not throttling — is what avoids the HTTP 407 blocking; a request that opens a fresh connection each time is what gets blocked.

5. **Short backoff, capped retries**
   4 attempts with a 3s-linear backoff plus jitter, so a few failing pages cannot stall the batch. A page that exhausts its retries is counted as a miss and lowers `completion_rate`, which is exactly what the safeguard gate measures.

### Measured result

| Model | Listings | Pages | Time |
|---|---|---|---|
| BMW 3시리즈 (G20) | 1,178 | 6 | 2.89s |
| BMW X3 (G01) | 637 | 4 | 1.29s |
| BMW X5 (G05) | 1,226 | 7 | 1.64s |
| **Total** | **3,041** | **17** | **5.81s** |

> Known trade-off: pagination sorts by `ModifiedDate`, so a listing that is bumped mid-crawl can shift between pages. Rows are deduplicated by `car_id`, and a shifted row can only ever cause a *missing* row, which the completion-rate and drop guards are there to absorb (the observed 1,178 vs. a reported count of 1,181 is this effect). If it ever matters, over-fetching with overlapping page windows is the fix.

---

## 6. Run schedule

- Runs as a periodic batch job (e.g. once daily, or per business requirement — to be aligned with the team)
- Each run refreshes `crawled_vehicles` to the latest snapshot, then runs the comparison logic

---

## 7. Deliverables

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | `crawled_vehicles` + `crawl_batches` migrations | Done | `status_crawl/migrations/001_crawl_tables.sql` |
| 2 | Encar listing crawler (direct API, no browser) | Done, tested live | `status_crawl/encar_stock_sync.py` |
| 3 | Per-model crawl config + `stock` scoping filter | Done — **`stock_filter` values are placeholders, need the real `stock` columns** | `status_crawl/models.json` |
| 4 | Comparison logic `stock` vs `crawled_vehicles` | Done, SQL verified; not yet run against a real DB | `compare_and_mark()` |
| 5 | Sold-status update + run logging | Done — every run writes a `crawl_batches` row with its metrics and outcome | `crawl_batches` |
| 6 | Completion-rate tracking + threshold gate (§4) | Done, gate matrix tested | `evaluate()` |
| 7 | Alerting on `PARTIAL` / `FAILED` batches | **Not done** — batches are recorded and logged, but nothing pages anyone yet | — |

### Running it

```bash
pip install requests pymysql python-dotenv

# Crawl only, no DB — smoke test
python status_crawl/encar_stock_sync.py --no-db --json output/snapshot.json

# Full run, but roll back — reports how many rows WOULD be marked SOLD
python status_crawl/encar_stock_sync.py --dry-run

# Full run
python status_crawl/encar_stock_sync.py

# One model
python status_crawl/encar_stock_sync.py --model bmw_3series_g20
```

### Before the first real run

1. Fill in `DB_HOST` / `DB_NAME` in `.env` (the file already has `DB_PORT` / `DB_USERNAME` / `DB_PASSWORD`)
2. Point `STOCK_*` env vars at the real `stock` column names
3. Replace the placeholder `stock_filter` in `models.json` with the real per-model condition
4. Apply `migrations/001_crawl_tables.sql`
5. Run with `--dry-run` first and sanity-check the SOLD count before letting it write
