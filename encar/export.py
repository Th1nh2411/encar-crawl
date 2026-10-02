"""
export.py -- JSON and CSV out of the database, on demand.

The database is where data lives; files are views of it taken when someone
needs one. Two views:

    cars      car_status -- one row per plate, Encar and stock side by side
    listings  vehicles   -- one row per Encar ad

Files are named <name>_<timestamp> in output/<view>/ so they never overwrite
each other. CSV is utf-8-sig so Excel opens the Korean plates correctly.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .config import OUTPUT_DIR

CAR_FIELDS = ("plate", "status", "model", "model_key", "year", "color_name", "mileage",
              "min_price", "max_price", "listings", "active_listings",
              "missing_streak", "in_stock", "product_id", "first_seen_at",
              "last_seen_at", "sold_detected_at", "car_ids")
LISTING_FIELDS = ("car_id", "vehicle_no", "model_key", "model_info", "form_year",
                  "mileage", "price", "color_name", "fuel_name", "is_duplicate",
                  "status", "missing_streak", "first_seen_at", "last_seen_at",
                  "sold_detected_at")


def query_rows(conn, view: str, model: Optional[str] = None,
               status: Optional[str] = None) -> List[Dict[str, Any]]:
    if view == "cars":
        table, fields, order = "car_status", CAR_FIELDS, "status, plate"
    else:
        table, fields, order = "vehicles", LISTING_FIELDS, "model_key, car_id"

    where, params = ["1=1"], []
    if model:
        where.append("model_key = ?")
        params.append(model)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = (f"SELECT {', '.join(fields)} FROM {table} WHERE {' AND '.join(where)} "
           f"ORDER BY {order}")
    return [dict(r) for r in conn.execute(sql, params)]


def save(view: str, name: str, rows: List[Dict[str, Any]], meta: Dict[str, Any],
         json_path: Optional[str] = None, csv_path: Optional[str] = None,
         out_dir: Optional[str] = None,
         fields: Optional[Sequence[str]] = None,
         flat_rows: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """Write wherever asked; with nothing asked, into output/<view>/.

    `flat_rows` is for rows whose JSON is nested: the CSV is written from it.
    """
    if not (json_path or csv_path or out_dir):
        out_dir = os.path.join(OUTPUT_DIR, view)
    if out_dir:
        base = os.path.join(out_dir, f"{name}_{datetime.now():%Y%m%d_%H%M%S}")
        json_path = json_path or f"{base}.json"
        csv_path = csv_path or f"{base}.csv"

    written: List[str] = []
    if json_path:
        _ensure_dir(json_path)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(envelope(view, name, rows, meta), fh, ensure_ascii=False, indent=2)
        written.append(json_path)
    if csv_path:
        rows = rows if flat_rows is None else flat_rows
        _ensure_dir(csv_path)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(fields or (rows[0] if rows else [])),
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        written.append(csv_path)
    return written


def envelope(view: str, name: str, rows: List[Dict[str, Any]],
             meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The JSON document: metadata first, rows last."""
    return {
        "view": view,
        "name": name,
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(rows),
        **(meta or {}),
        "rows": rows,
    }


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
