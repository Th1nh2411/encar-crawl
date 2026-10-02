"""
parse.py -- The one row shape every route produces, and how to fill it.

A row is one Encar listing (`car_id`). Two sources fill it:

    listing   the search result: model, price, mileage, year, fuel ...
              -- comes free with every page, changes between runs
    detail    the per-car record: the vehicle number (the field the listing
              does not carry), plus grade, colour and the sharper model name
              -- one request per car, and does not change for a listing

The detail arrives as JSON from the API routes and as JSON embedded in the
detail page's HTML from the browser route; both are read here into the same
fields. The collector only returns car number and model info, so its rows
leave the listing fields empty -- the store merges rather than overwrites, so
a sparse row never blanks what a richer route stored earlier.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

Row = Dict[str, Any]

# Column order for CSV exports and the `vehicles` table.
FIELDS = ("car_id", "vehicle_no", "model_info", "manufacturer", "model_group",
          "model_name", "grade_name", "form_year", "year_month", "mileage",
          "fuel_name", "transmission", "color_name", "price", "is_duplicate")

# What the detail record decides and what therefore never changes while the
# listing exists. A stored row may supply these to a fresh crawl so the car is
# not asked about again. `mileage` and `price` are deliberately absent: they
# move, and the listing this run just read is the current answer.
DETAIL_FIELDS = ("vehicle_no", "model_info", "manufacturer", "model_group",
                 "model_name", "grade_name", "form_year", "year_month",
                 "fuel_name", "transmission", "color_name")

DETAIL_PAGE = "https://fem.encar.com/cars/detail/{car_id}"


def as_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def empty_row(car_id: Any, page: Optional[int] = None) -> Row:
    row: Row = {k: None for k in FIELDS}
    row.update(car_id=str(car_id), is_duplicate=None, page=page)
    return row


def listing_model_info(item: Dict[str, Any]) -> Optional[str]:
    """Readable model string from a listing result: make + model + badge."""
    joined = " ".join(str(p) for p in (item.get("Manufacturer"), item.get("Model"),
                                       item.get("Badge")) if p).strip()
    return joined or None


def listing_row(item: Dict[str, Any], page: Optional[int] = None) -> Row:
    """The fields one search result carries."""
    row = empty_row(item.get("Id"), page)
    year = str(item.get("Year") or "")
    row.update(
        model_info=listing_model_info(item),
        manufacturer=item.get("Manufacturer"),
        model_name=item.get("Model"),
        grade_name=item.get("Badge"),
        form_year=_str(item.get("FormYear")),
        year_month=year.split(".")[0] or None,
        mileage=as_int(item.get("Mileage")),
        fuel_name=item.get("FuelType"),
        transmission=item.get("Transmission"),
        price=as_int(item.get("Price")),
        is_duplicate=1 if item.get("ServiceCopyCar") == "DUPLICATION" else 0,
    )
    return row


def detail_fields(detail: Dict[str, Any]) -> Dict[str, Any]:
    """What the detail record adds, from the API's JSON.

    Asked for with `include=CATEGORY,SPEC`: ~1.2 KB against ~11 KB for the full
    record, and still carrying the vehicle number, the category block and the
    colour/fuel/mileage spec block.
    """
    category = detail.get("category") or {}
    spec = detail.get("spec") or {}
    return {
        "vehicle_no": detail.get("vehicleNo"),
        "manufacturer": category.get("manufacturerName"),
        "model_group": category.get("modelGroupName"),
        "model_name": category.get("modelName"),
        "grade_name": category.get("gradeName"),
        "grade_detail": category.get("gradeDetailName"),
        "form_year": category.get("formYear"),
        "year_month": category.get("yearMonth"),
        "color_name": spec.get("colorName"),
        "fuel_name": spec.get("fuelName"),
        "transmission": spec.get("transmissionName"),
        "mileage": spec.get("mileage"),
    }


# The detail page is one big JSON blob inside markup, so fields are pulled out
# by name rather than by parsing a document with no stable shape. The first
# match is the car itself; recommended cars further down repeat the keys.
_HTML_KEYS = {
    "vehicleNo": "vehicle_no",
    "manufacturerName": "manufacturer",
    "modelGroupName": "model_group",
    "modelName": "model_name",
    "gradeName": "grade_name",
    "gradeDetailName": "grade_detail",
    "formYear": "form_year",
    "yearMonth": "year_month",
    "colorName": "color_name",
    "fuelName": "fuel_name",
    "transmissionName": "transmission",
}
_HTML_RE = re.compile(r'"(%s)"\s*:\s*"?([^",}\]]*)' % "|".join(_HTML_KEYS))


def detail_fields_from_html(html: str) -> Dict[str, Any]:
    """The same fields as detail_fields(), out of the server-rendered page."""
    out: Dict[str, Any] = {}
    for key, value in _HTML_RE.findall(html):
        field = _HTML_KEYS[key]
        value = value.strip()
        if field not in out and value and value != "null":
            out[field] = value
    return out


def apply_detail(row: Row, fields: Dict[str, Any]) -> None:
    """Overlay detail fields onto a row. Empty values never erase listing ones."""
    for key in ("vehicle_no", "manufacturer", "model_group", "model_name",
                "grade_name", "form_year", "year_month", "color_name",
                "fuel_name", "transmission"):
        value = fields.get(key)
        if value not in (None, ""):
            row[key] = str(value)

    mileage = as_int(fields.get("mileage"))
    if mileage is not None:
        row["mileage"] = mileage

    # The sharper model string: the detail carries the grade detail the
    # listing's Badge does not.
    info = " ".join(str(fields[k]) for k in
                    ("manufacturer", "model_name", "grade_name", "grade_detail")
                    if fields.get(k)).strip()
    if info:
        row["model_info"] = info


def car_id_from_url(url: str) -> Optional[str]:
    """`.../cars/detail/42688114` or `...?carid=42688114` -> '42688114'."""
    match = re.search(r"(?:carid=|/detail/)(\d+)", url or "")
    return match.group(1) if match else None


def _str(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def split_known(rows, known: Dict[str, Dict[str, Any]]) -> Tuple[list, list]:
    """Fill rows from what is already stored; return (filled, still to fetch).

    A listing keeps its vehicle number for as long as it exists, so one that
    has been looked up needs no detail request at all.
    """
    filled, missing = [], []
    for row in rows:
        entry = known.get(row["car_id"])
        if not entry or not entry.get("vehicle_no"):
            missing.append(row)
            continue
        for key in DETAIL_FIELDS:
            value = entry.get(key)
            if value not in (None, ""):
                row[key] = value
        filled.append(row)
    return filled, missing
