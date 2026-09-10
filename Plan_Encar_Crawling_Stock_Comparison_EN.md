# Plan: Encar Model-Specific Crawling & Stock "Sold" Detection

## 1. Purpose

Crawl listings for a specific model from Encar and compare the results against our internal `stock` table.
Any vehicle number that exists in our `stock` table but no longer appears in the crawled data should be **marked as sold**.

The previous approach crawled full listing detail pages, which made the process too slow.
For this plan, the crawl target is much smaller (only 2 fields), so **the top priority is minimizing crawl time** through a lightweight approach.

---

## 2. Scope

### Data to crawl (only 2 fields)

| Field | Description |
|---|---|
| Vehicle number (차량번호) | Used as the matching key |
| Model info (모델정보) | Model name level detail — trim/year-level detail is not required |

> Price, options, photos, dealer info, etc. from detail pages are **not** collected. Limiting the scope to these two fields is what makes the crawl lightweight.

### Process flow

1. Crawl Encar's search/listing results filtered to a specific model → extract only vehicle number + model info
2. Store the crawled results in a separate table (`crawled_vehicles`, working name)
3. Compare `crawled_vehicles` against our `stock` table, matched by vehicle number
4. Any vehicle number present in `stock` but absent from `crawled_vehicles` → **update status to SOLD**

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
| vehicle_no | varchar, index | Vehicle number |
| model_info | varchar | Model info |
| crawl_batch_id | varchar/int | Identifier for this crawl run (used in the comparison logic) |
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

- Keep the existing structure; join/compare on the vehicle number column
- Assumes a status field already exists to represent "sold"

---

## 4. Comparison (matching) logic

```
for each vehicle_no in stock (status != SOLD):
    if vehicle_no not in crawled_vehicles(latest batch):
        stock.status = SOLD
        stock.sold_confirmed_at = now()
```

- This can be implemented as a simple LEFT JOIN / NOT IN query
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

The old approach was slow because it visited **individual detail pages** to pull large amounts of data.
Since this plan only needs 2 fields (vehicle number, model info), optimize as follows:

1. **Collect only from the listing/search results page — do not visit detail pages**
   Vehicle number and model info are typically already visible on the search results list (card UI). Confirm (via manual check before development starts) whether both fields can be fully extracted from the listing page alone, without opening detail pages.

2. **Prefer calling the internal API (XHR/JSON) directly over rendering full HTML**
   Check browser dev tools for an internal API called when the search results load. If one exists, call it directly and parse JSON — much lighter and faster than HTML parsing.

3. **Scope the crawl to a specific model filter**
   Use a search URL/query already filtered to the target model, rather than crawling all listings, to minimize the number of pages to traverse.

4. **Block unnecessary resource loading** (if using a browser-based crawler)
   Disable loading of images, CSS, fonts, ad scripts, etc. that aren't needed for parsing (Bright Data's Scraping Browser supports resource-blocking options).

5. **Increase parallelism**
   Listing pages are lighter and lower-risk to fetch than detail pages, so concurrency can be increased to reduce total crawl time — but test to find a safe concurrency level, since excessive parallelism raises blocking risk.

6. **Minimize unnecessary retries/wait times**
   Use short backoff instead of immediate retries on failure, and tune timeout values so a handful of failing pages don't delay the entire batch.

> Before development starts, it's recommended to run a **single-page sample test** to confirm that vehicle number + model info can be fully extracted from the listing page alone, and to finalize the crawling approach based on that result.

---

## 6. Run schedule

- Runs as a periodic batch job (e.g. once daily, or per business requirement — to be aligned with the team)
- Each run refreshes `crawled_vehicles` to the latest snapshot, then runs the comparison logic

---

## 7. Deliverables

1. `crawled_vehicles` + `crawl_batches` table migrations
2. Encar listing-page crawler (Bright Data integration, lightweight approach as described above)
3. Batch comparison logic: `stock` vs `crawled_vehicles`
4. Sold-status update logic with logging/alerting
5. Crawl completion-rate tracking + threshold gate before the comparison logic (§4), with alerts on `PARTIAL` / `FAILED` batches
