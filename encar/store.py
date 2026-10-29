"""
store.py -- The database: one SQLite file, var/encar.db, and everything in it.

    vehicles        one row per Encar listing (car_id). Every route writes here,
                    merging: a sparse row (the collector's two fields) never
                    blanks what a richer route stored. Also the car-number
                    cache -- a listing whose vehicle_no is stored is never
                    asked about again (see known_details).
    crawl_batches   one row per model per crawl: which route delivered, what it
                    covered, whether sold detection ran and what it marked.
    route_state     which routes are parked after a block, and until when.
    sync_runs       one row per sync-sold run (partners.py): what partners-stock
                    listed, what Encar still shows, what was reported sold.
    product_group   the sonokong vehicle taxonomy  } imported from data/ by
    rent_product    the sonokong live stock        } stock_import.py

    car_status      THE view to read. One row per plate, joining what Encar
                    shows with what is in stock, with one status vocabulary:

        ON_SALE     판매중    at least one ad live, seen in the latest crawl
        CHECKING    확인중    still marked live, but missing from the latest
                              crawl(s) -- one more miss and it is SOLD
        SOLD        판매완료  every ad for the plate is gone
        NOT_FOUND   미확인    in stock, never seen in any crawl -- outside the
                              models crawled, not evidence of a sale

The schema is created on connect and is safe to re-run. Timestamps are local
time, 'YYYY-MM-DD HH:MM:SS' text, which sorts and compares correctly.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import DB_PATH
from .parse import DETAIL_FIELDS, FIELDS, Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    car_id           TEXT PRIMARY KEY,          -- Encar listing id
    vehicle_no       TEXT,                      -- plate, from the detail record
    model_key        TEXT NOT NULL,             -- models.json key that last saw it
    model_info       TEXT,
    manufacturer     TEXT,
    model_group      TEXT,
    model_name       TEXT,
    grade_name       TEXT,
    form_year        TEXT,
    year_month       TEXT,
    mileage          INTEGER,
    fuel_name        TEXT,
    transmission     TEXT,
    color_name       TEXT,
    price            INTEGER,                   -- 만원
    is_duplicate     INTEGER NOT NULL DEFAULT 0,-- ServiceCopyCar = DUPLICATION
    status           TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SOLD')),
    missing_streak   INTEGER NOT NULL DEFAULT 0,-- consecutive healthy crawls without it
    last_missing_at  TEXT,
    sold_detected_at TEXT,                      -- when it was found gone, not the sale date
    last_batch_id    INTEGER,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    detail_at        TEXT                       -- when vehicle_no was last looked up
);
CREATE INDEX IF NOT EXISTS idx_vehicles_plate ON vehicles (vehicle_no);
CREATE INDEX IF NOT EXISTS idx_vehicles_model_status ON vehicles (model_key, status);

CREATE TABLE IF NOT EXISTS crawl_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key         TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'RUNNING'
                      CHECK (status IN ('RUNNING', 'OK', 'PARTIAL', 'FAILED')),
    route             TEXT,                     -- the route that delivered
    attempts          TEXT,                     -- JSON: what every route did
    every_page        INTEGER NOT NULL DEFAULT 0,
    listing_total     INTEGER,                  -- Count reported by Encar
    pages_requested   INTEGER,
    pages_fetched     INTEGER,
    completion_rate   REAL,
    crawled_count     INTEGER NOT NULL DEFAULT 0,
    vehicle_no_filled INTEGER NOT NULL DEFAULT 0,
    sold_checked      INTEGER NOT NULL DEFAULT 0,
    missing_count     INTEGER NOT NULL DEFAULT 0,
    sold_marked_count INTEGER NOT NULL DEFAULT 0,
    note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_model ON crawl_batches (model_key, status, id);

CREATE TABLE IF NOT EXISTS route_state (
    route    TEXT PRIMARY KEY,
    at       REAL NOT NULL,
    cooldown REAL NOT NULL,
    why      TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    source          TEXT NOT NULL,              -- cron (main.py) / manual (dashboard API)
    target          TEXT,                       -- QA / TEST / LIVE
    server          TEXT,                       -- partners-stock host
    dry_run         INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('OK', 'FAILED')),
    checked_ids     TEXT,                       -- JSON: active on partners-stock
    active_ids      TEXT,                       -- JSON: still advertised on Encar
    sold_ids        TEXT,                       -- JSON: sent to update-car-id
    error_ids       TEXT,                       -- JSON: could not be checked
    reported        INTEGER NOT NULL DEFAULT 0, -- update-car-id was called and answered
    update_response TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS product_group (
    group_id        INTEGER PRIMARY KEY,
    upper_id        INTEGER DEFAULT 0,          -- parent; 0 at the root
    name            TEXT,
    group_code      TEXT,
    brand_nt        TEXT,
    model_nt        TEXT,
    model_detail_nt TEXT,
    is_del          INTEGER NOT NULL DEFAULT 0,
    is_enabled      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rent_product (
    product_id           INTEGER PRIMARY KEY,   -- rent_product.id in sonokong
    car_number           TEXT NOT NULL,         -- joins to vehicles.vehicle_no
    plate_valid          INTEGER NOT NULL,      -- 0 for QA placeholders
    vin                  TEXT,
    group_id             INTEGER,
    model_name           TEXT,                  -- product_group path, built at import
    car_manufacture_date TEXT,
    mileage              INTEGER,
    purchase_price       INTEGER,               -- KRW
    engine_displacement  INTEGER,
    car_fuel             TEXT,
    ex_color             TEXT,
    car_region           TEXT,
    car_origin           TEXT,
    car_status           TEXT,                  -- rental state, not the sale status
    web_visibility       TEXT,
    updated_at           TEXT,
    imported_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rent_product_plate ON rent_product (car_number);

DROP VIEW IF EXISTS car_status;
CREATE VIEW car_status AS
WITH enc AS (
    SELECT
        vehicle_no                                            AS plate,
        MAX(model_key)                                        AS model_key,
        MAX(model_info)                                       AS model_info,
        MAX(form_year)                                        AS form_year,
        MAX(color_name)                                       AS color_name,
        MAX(mileage)                                          AS mileage,
        COUNT(*)                                              AS listings,
        SUM(status = 'ACTIVE')                                AS active_listings,
        -- Seen in the latest crawl if any live ad has no miss against it.
        MIN(CASE WHEN status = 'ACTIVE' THEN missing_streak END) AS missing_streak,
        MIN(CASE WHEN status = 'ACTIVE' THEN price END)       AS min_price,
        MAX(CASE WHEN status = 'ACTIVE' THEN price END)       AS max_price,
        MIN(first_seen_at)                                    AS first_seen_at,
        MAX(last_seen_at)                                     AS last_seen_at,
        MAX(sold_detected_at)                                 AS sold_detected_at,
        GROUP_CONCAT(car_id)                                  AS car_ids
    FROM vehicles
    WHERE vehicle_no IS NOT NULL AND vehicle_no <> ''
    GROUP BY vehicle_no
),
stk AS (
    SELECT
        car_number                                            AS plate,
        MIN(product_id)                                       AS product_id,
        MAX(model_name)                                       AS model_name,
        MAX(substr(car_manufacture_date, 1, 4))               AS model_year,
        MAX(mileage)                                          AS mileage,
        MAX(purchase_price)                                   AS purchase_price,
        MAX(car_status)                                       AS car_status,
        MIN(plate_valid)                                      AS plate_valid
    FROM rent_product
    GROUP BY car_number
),
-- Every plate from either side: SQLite's FULL JOIN spelled as two LEFT JOINs.
plates AS (
    SELECT plate FROM enc
    UNION
    SELECT plate FROM stk
)
SELECT
    p.plate,
    CASE
        WHEN enc.plate IS NULL        THEN 'NOT_FOUND'
        WHEN enc.active_listings = 0  THEN 'SOLD'
        WHEN enc.missing_streak > 0   THEN 'CHECKING'
        ELSE                               'ON_SALE'
    END                                                       AS status,
    COALESCE(enc.model_info, stk.model_name)                  AS model,
    enc.model_key,
    COALESCE(enc.form_year, stk.model_year)                   AS year,
    enc.color_name,
    COALESCE(enc.mileage, stk.mileage)                        AS mileage,
    enc.min_price,
    enc.max_price,
    COALESCE(enc.listings, 0)                                 AS listings,
    COALESCE(enc.active_listings, 0)                          AS active_listings,
    COALESCE(enc.missing_streak, 0)                           AS missing_streak,
    stk.plate IS NOT NULL                                     AS in_stock,
    stk.product_id,
    stk.purchase_price,
    stk.car_status,
    COALESCE(stk.plate_valid, 1)                              AS plate_valid,
    enc.first_seen_at,
    enc.last_seen_at,
    enc.sold_detected_at,
    enc.car_ids
FROM plates p
LEFT JOIN enc ON enc.plate = p.plate
LEFT JOIN stk ON stk.plate = p.plate;
"""

STATUSES = ("ON_SALE", "CHECKING", "SOLD", "NOT_FOUND")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    """Open the database, creating it and its schema if needed."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # check_same_thread off: the dashboard's request threads each open their
    # own connection, but a crawl may hand one between helper threads.
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets the dashboard read while a crawl writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Columns added after a table was first created, which CREATE IF NOT EXISTS skips."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sync_runs)")}
    if "target" not in cols:
        conn.execute("ALTER TABLE sync_runs ADD COLUMN target TEXT")
        # Every run before servers were named was against QA.
        conn.execute("UPDATE sync_runs SET target = 'QA'")
        conn.commit()


# ── Car numbers already known ─────────────────────────────────────────────────

def known_details(conn: sqlite3.Connection, car_ids: Optional[Iterable[str]] = None,
                  max_age_days: float = 0.0) -> Dict[str, Dict[str, Any]]:
    """Stored detail fields per car_id, for listings that have a vehicle number.

    A listing keeps its vehicle number for as long as it exists, so these cars
    cost no detail request. Entries looked up more than `max_age_days` ago are
    left out, so they are fetched again: nothing else would ever correct one.
    """
    sql = (f"SELECT car_id, {', '.join(DETAIL_FIELDS)} FROM vehicles "
           f"WHERE vehicle_no IS NOT NULL AND vehicle_no <> ''")
    params: List[Any] = []
    if max_age_days:
        cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime(
            "%Y-%m-%d %H:%M:%S")
        sql += " AND (detail_at IS NULL OR detail_at >= ?)"
        params.append(cutoff)

    wanted = None if car_ids is None else {str(c) for c in car_ids}
    out: Dict[str, Dict[str, Any]] = {}
    for row in conn.execute(sql, params):
        if wanted is None or row["car_id"] in wanted:
            out[row["car_id"]] = {k: row[k] for k in DETAIL_FIELDS}
    return out


# ── crawl_batches ─────────────────────────────────────────────────────────────

def open_batch(conn: sqlite3.Connection, model_key: str) -> int:
    cur = conn.execute(
        "INSERT INTO crawl_batches (model_key, started_at) VALUES (?, ?)",
        (model_key, now()))
    conn.commit()
    return int(cur.lastrowid or 0)


def close_batch(conn: sqlite3.Connection, batch_id: int, **fields: Any) -> None:
    fields.setdefault("finished_at", now())
    if isinstance(fields.get("attempts"), (list, dict)):
        fields["attempts"] = json.dumps(fields["attempts"], ensure_ascii=False)
    if fields.get("note"):
        fields["note"] = str(fields["note"])[:500]
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE crawl_batches SET {cols} WHERE id = ?",
                 (*fields.values(), batch_id))


def fail_batch(conn: sqlite3.Connection, batch_id: int, note: str,
               attempts: Optional[Sequence[Dict[str, str]]] = None) -> None:
    """Close a batch as FAILED. Best effort: never mask the original error."""
    try:
        conn.rollback()
        close_batch(conn, batch_id, status="FAILED", note=note,
                    attempts=list(attempts or []))
        conn.commit()
    except sqlite3.Error:
        pass


def save_sync_run(conn: sqlite3.Connection, run: Dict[str, Any]) -> None:
    """Store one sync-sold run. List values are kept as JSON."""
    run = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
           for k, v in run.items()}
    if run.get("note"):
        run["note"] = str(run["note"])[:500]
    cols = ", ".join(run)
    conn.execute(f"INSERT INTO sync_runs ({cols}) VALUES ({', '.join('?' * len(run))})",
                 tuple(run.values()))
    conn.commit()


def previous_ok_count(conn: sqlite3.Connection, model_key: str) -> Optional[int]:
    row = conn.execute(
        "SELECT crawled_count FROM crawl_batches WHERE model_key = ? AND status = 'OK' "
        "ORDER BY id DESC LIMIT 1", (model_key,)).fetchone()
    return int(row[0]) if row else None


# ── vehicles ──────────────────────────────────────────────────────────────────

_MERGED = [f for f in FIELDS if f not in ("car_id", "is_duplicate")]

UPSERT_SQL = f"""
INSERT INTO vehicles ({', '.join(FIELDS)}, model_key, status, missing_streak,
                      last_batch_id, first_seen_at, last_seen_at, detail_at)
VALUES ({', '.join(':' + f for f in FIELDS[:-1])}, COALESCE(:is_duplicate, 0),
        :model_key, 'ACTIVE', 0, :batch_id, :seen_at, :seen_at, :detail_at)
ON CONFLICT (car_id) DO UPDATE SET
    {', '.join(f'{f} = COALESCE(excluded.{f}, {f})' for f in _MERGED)},
    is_duplicate     = COALESCE(:is_duplicate, is_duplicate),
    model_key        = excluded.model_key,
    detail_at        = COALESCE(:detail_at, detail_at),
    last_seen_at     = excluded.last_seen_at,
    last_batch_id    = excluded.last_batch_id,
    -- Seen again => live again. A relisted car loses its SOLD flag, and a
    -- listing that blinked out for one run loses its streak.
    status           = 'ACTIVE',
    missing_streak   = 0,
    sold_detected_at = NULL
"""


def upsert_vehicles(conn: sqlite3.Connection, model_key: str, rows: Sequence[Row],
                    batch_id: Optional[int], fresh_ids: Iterable[str] = ()) -> None:
    """Store a crawl's rows, merging into what is already there.

    `fresh_ids` are the cars whose vehicle number was looked up this run; their
    `detail_at` moves forward, which is what the max-age refresh counts from.
    """
    seen_at = now()
    fresh = set(fresh_ids)
    conn.executemany(UPSERT_SQL, [
        {**{f: r.get(f) for f in FIELDS}, "model_key": model_key,
         "batch_id": batch_id, "seen_at": seen_at,
         "detail_at": seen_at if r["car_id"] in fresh and r.get("vehicle_no") else None}
        for r in rows
    ])


def mark_missing(conn: sqlite3.Connection, model_key: str, batch_id: int,
                 sold_after: int) -> Tuple[int, int]:
    """Listings of this model the batch did not see. -> (missing now, newly SOLD)

    One absence is not a sale -- a dealer pauses an ad, a page comes back short
    while still answering 200 -- so a miss only bumps the streak, and only a
    streak of `sold_after` flips the listing to SOLD.

    Only rows whose model_key is this model are candidates: a row last seen by
    an overlapping model's crawl belongs to that model until this one sees it.
    """
    at = now()
    missing = conn.execute(
        "UPDATE vehicles SET missing_streak = missing_streak + 1, last_missing_at = ? "
        "WHERE model_key = ? AND status <> 'SOLD' "
        "AND (last_batch_id IS NULL OR last_batch_id <> ?)",
        (at, model_key, batch_id)).rowcount
    sold = conn.execute(
        "UPDATE vehicles SET status = 'SOLD', sold_detected_at = ? "
        "WHERE model_key = ? AND status <> 'SOLD' AND missing_streak >= ?",
        (at, model_key, sold_after)).rowcount
    return int(missing), int(sold)


# ── route_state ───────────────────────────────────────────────────────────────

def load_route_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    return {r["route"]: {"at": r["at"], "cooldown": r["cooldown"], "why": r["why"]}
            for r in conn.execute("SELECT * FROM route_state")}


def save_route_state(conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
    conn.execute("DELETE FROM route_state")
    conn.executemany(
        "INSERT INTO route_state (route, at, cooldown, why) VALUES (?, ?, ?, ?)",
        [(k, v["at"], v["cooldown"], v.get("why")) for k, v in state.items()
         if isinstance(v, dict) and "at" in v and "cooldown" in v])
    conn.commit()


# ── One-off: the JSON plate cache the file-only exporter kept ─────────────────

def import_plate_cache(conn: sqlite3.Connection, path: str) -> int:
    """Seed `vehicles` from var/plate_cache.json, so those cars are not re-fetched.

    Each entry was a listing live when it was cached, so it goes in as ACTIVE.
    The next full crawl of its model refreshes the ones still listed and starts
    the missing streak on the rest -- exactly what a listing seen once and then
    gone should get. Existing rows are left alone. Returns rows added.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0

    rows = []
    for car_id, entry in data.items():
        if not isinstance(entry, dict) or not entry.get("car_number"):
            continue
        at = entry.get("at")
        stamp = (datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")
                 if isinstance(at, (int, float)) else now())
        rows.append((str(car_id), entry["car_number"], entry.get("model") or "unknown",
                     entry.get("model_info") or None, stamp, stamp, stamp))

    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO vehicles (car_id, vehicle_no, model_key, model_info, "
        "first_seen_at, last_seen_at, detail_at) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return conn.total_changes - before


# ── Stock ─────────────────────────────────────────────────────────────────────

def replace_stock(conn: sqlite3.Connection, groups: List[Dict[str, Any]],
                  products: List[Dict[str, Any]]) -> None:
    """Swap in a fresh import of the sonokong tables, in one transaction."""
    def insert(table: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        cols = list(rows[0])
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            [tuple(r.get(c) for c in cols) for r in rows])

    with conn:
        conn.execute("DELETE FROM product_group")
        conn.execute("DELETE FROM rent_product")
        insert("product_group", groups)
        insert("rent_product", products)


def status_counts(conn: sqlite3.Connection, where: str = "plate_valid = 1",
                  params: Sequence[Any] = ()) -> Dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for r in conn.execute(f"SELECT status, COUNT(*) AS n FROM car_status "
                          f"WHERE {where} GROUP BY status", params):
        counts[r["status"]] = r["n"]
    return counts
