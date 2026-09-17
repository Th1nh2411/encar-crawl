"""
results.py -- Writing a run's rows out, the same way every tool in this project
writes them.

One shape, so a file from listing_export, brightdata_collector or
stock_pipeline can be diffed against another without reshaping it first:

    {
      "tool": "stock_pipeline",
      "name": "bmw_3series_g20",
      "crawledAt": "2026-09-17T11:13:44+09:00",
      "count": 60,
      ... run metadata ...
      "rows": [ {...}, ... ]
    }

and a CSV of the same rows, utf-8-sig so Excel opens the Korean plates and
model names correctly.

`save()` writes whatever paths it is given. With `out_dir` it names the files
itself -- `<name>_<timestamp>.json` / `.csv` -- so repeated runs never overwrite
each other. Nothing is written when no destination is asked for.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

# The row schema this tool produces, in column order: one row per model run.
ROW_FIELDS = ("model_key", "status", "crawled_count", "vehicle_no_filled",
              "completion_rate", "fetched_pages", "total_expected_pages",
              "missing_now", "sold_marked", "note")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def envelope(tool: str, name: str, rows: List[Dict[str, Any]],
             meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The JSON document: metadata first, rows last."""
    return {
        "tool": tool,
        "name": name,
        "crawledAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(rows),
        **(meta or {}),
        "rows": rows,
    }


def write_json(path: str, payload: Dict[str, Any]) -> str:
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def write_csv(path: str, rows: List[Dict[str, Any]],
              fields: Sequence[str] = ROW_FIELDS) -> str:
    _ensure_dir(path)
    # utf-8-sig so Excel opens the Korean plates and model names correctly.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def save(tool: str, name: str, rows: List[Dict[str, Any]],
         meta: Optional[Dict[str, Any]] = None, *,
         json_path: Optional[str] = None, csv_path: Optional[str] = None,
         out_dir: Optional[str] = None,
         fields: Sequence[str] = ROW_FIELDS,
         flat_rows: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """Write the run wherever it was asked for. Returns the paths written.

    `flat_rows` is for the runs whose JSON rows are nested: CSV has no place to
    put a list, so those callers hand in a flattened view for the CSV alone.
    """
    payload = envelope(tool, name, rows, meta)
    written: List[str] = []

    if out_dir:
        base = os.path.join(out_dir, f"{name}_{timestamp()}")
        json_path = json_path or f"{base}.json"
        csv_path = csv_path or f"{base}.csv"

    if json_path:
        written.append(write_json(json_path, payload))
    if csv_path:
        written.append(write_csv(csv_path, rows if flat_rows is None else flat_rows,
                                 fields))
    return written


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
