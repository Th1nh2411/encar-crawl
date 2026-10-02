"""
stock_import.py — Load the sonokong `product_group` and `rent_product` dumps
from data/ into the database.

Both dumps are data-only: `INSERT INTO <table> VALUES (...)` with no column
list and no CREATE TABLE. The matching `.sch` files carry the real schema, so
column positions come from parsing those rather than from guessing at values.

    product_group.sch + product_group.sql.gz   ->  product_group
    rent_product.sch  + rent_product.sql.gz    ->  rent_product

product_group is a tree (`upper_id` -> parent, 0 at the root). A car's readable
model name is the root-to-node path, so it is built here and materialised into
`rent_product.model_name`: the taxonomy is static, and a stored path keeps the
dashboard's queries flat.

Only live stock is imported (`is_del = 0`) — a soft-deleted car is out of
inventory, so whether Encar still lists it is not a question worth asking.

    python main.py import-stock
    python main.py import-stock --all        # include is_del = 1
    python main.py import-stock --dry-run
"""

from __future__ import annotations

import gzip
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import store
from .log import log

# A real Korean plate: 12가3456 / 123가4567. 21 live rows carry QA placeholders
# ('test 0101', 'For Test 02', '1') instead; they can never match Encar, so
# they are flagged at import and hidden by default.
PLATE_RE = re.compile(r"^[0-9]{2,3}[가-힣][0-9]{4}$")

TUPLE_RE = re.compile(r"\((.*?)\)(?=,\(|$)", re.S)
COL_RE = re.compile(r"^\s*`([A-Za-z0-9_]+)`\s", re.M)

# Column in the source table -> column here. Everything else in the 90-column
# rent_product row (insurance, contracts, exposure flags, paperwork) is dropped.
RENT_PRODUCT_COLS = {
    "id": "product_id",
    "car_number": "car_number",
    "vin": "vin",
    "group_id": "group_id",
    "car_manufacture_date": "car_manufacture_date",
    "mileage": "mileage",
    "purchase_price": "purchase_price",
    "engine_displacement": "engine_displacement",
    "car_fuel": "car_fuel",
    "ex_color": "ex_color",
    "car_region": "car_region",
    "car_origin": "car_origin",
    "car_status": "car_status",
    "web_visibility": "web_visibility",
    "updated_at": "updated_at",
}

PRODUCT_GROUP_COLS = {
    "group_id": "group_id",
    "upper_id": "upper_id",
    "name": "name",
    "group_code": "group_code",
    "brand_nt": "brand_nt",
    "model_nt": "model_nt",
    "model_detail_nt": "model_detail_nt",
    "is_del": "is_del",
    "is_enabled": "is_enabled",
}

INT_FIELDS = {
    "product_id", "group_id", "upper_id", "mileage", "purchase_price",
    "engine_displacement", "is_del", "is_enabled",
}
DATE_FIELDS = {"car_manufacture_date", "updated_at"}


def schema_columns(sch_path: str, table: str) -> List[str]:
    """Column order from a mysqldump .sch file — the dumps have no column list.

    Generated columns are included: mysqldump emits a placeholder for them in
    the INSERT tuple, so skipping one here would shift every later column.
    """
    with open(sch_path, encoding="utf-8", errors="replace") as f:
        sql = f.read()
    marker = f"CREATE TABLE `{table}` ("
    start = sql.index(marker) + len(marker)
    body = sql[start:]
    # Stop at the first line that starts a key/constraint definition.
    end = re.search(r"^\s*(PRIMARY KEY|UNIQUE KEY|KEY|CONSTRAINT|\) ENGINE)", body, re.M)
    body = body[:end.start()] if end else body
    return COL_RE.findall(body)


def split_tuple(body: str) -> List[str]:
    """Split one VALUES tuple on commas that are not inside a quoted string."""
    out: List[str] = []
    cur: List[str] = []
    quoted = escaped = False
    for ch in body:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            cur.append(ch)
            escaped = True
        elif ch == "'":
            quoted = not quoted
            cur.append(ch)
        elif ch == "," and not quoted:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


def unquote(raw: str) -> Optional[str]:
    if raw in ("NULL", ""):
        return None
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        raw = raw[1:-1]
        # \\ must be last so it cannot eat the escapes handled before it.
        for a, b in (("\\'", "'"), ('\\"', '"'), ("\\n", "\n"),
                     ("\\r", "\r"), ("\\0", ""), ("\\\\", "\\")):
            raw = raw.replace(a, b)
    return raw


def parse_dump(dump: str, table: str, columns: List[str]) -> List[Dict[str, Any]]:
    prefix = f"INSERT INTO `{table}` VALUES"
    rows: List[Dict[str, Any]] = []
    width_mismatch = 0
    opener = gzip.open if dump.endswith(".gz") else open
    with opener(dump, "rt", encoding="utf-8", errors="replace") as f:  # type: ignore[operator]
        for line in f:
            if not line.startswith(prefix):
                continue
            body = line[line.index("VALUES") + 6:].rstrip().rstrip(";")
            for tup in TUPLE_RE.findall(body):
                values = [unquote(v) for v in split_tuple(tup)]
                if len(values) != len(columns):
                    width_mismatch += 1
                    continue
                rows.append(dict(zip(columns, values)))
    if width_mismatch:
        log(f"  ! {width_mismatch} row(s) skipped: column count did not match the schema")
    return rows


def coerce(field: str, value: Optional[str]) -> Any:
    if value is None:
        return None
    if field in INT_FIELDS:
        try:
            return int(float(value))
        except ValueError:
            return None
    if field in DATE_FIELDS and value.startswith("0000"):
        return None
    return value


def build_paths(groups: List[Dict[str, Any]]) -> Dict[int, str]:
    """Root-to-node label path for every group, e.g. '기아 쏘렌토 ... 1.6 HEV_노블레스'."""
    by_id = {int(g["group_id"]): g for g in groups if g.get("group_id") is not None}
    cache: Dict[int, str] = {}

    def path(gid: int, depth: int = 0) -> str:
        if gid in cache:
            return cache[gid]
        node = by_id.get(gid)
        if node is None or depth > 8:  # depth cap guards a cycle in the data
            return ""
        parent = node.get("upper_id")
        parent_path = ""
        if parent not in (None, 0, "0") and int(parent) != gid:
            parent_path = path(int(parent), depth + 1)
        label = (node.get("name") or "").strip()
        full = f"{parent_path} {label}".strip()
        cache[gid] = full
        return full

    return {gid: path(gid) for gid in by_id}


def load_table(dump: str, sch: str, table: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    columns = schema_columns(sch, table)
    log(f"  {table}: {len(columns)} column(s) in {os.path.basename(sch)}")
    rows = parse_dump(dump, table, columns)
    log(f"  {table}: {len(rows)} row(s) parsed")
    return rows, columns


def import_stock(conn, data_dir: str, include_deleted: bool = False,
                 dry_run: bool = False) -> int:
    """Parse both dumps and replace the stock tables. Returns rows imported."""
    paths = {name: (os.path.join(data_dir, f"{name}.sql.gz"),
                    os.path.join(data_dir, f"{name}.sch"))
             for name in ("product_group", "rent_product")}
    for name, (dump, sch) in paths.items():
        for p in (dump, sch):
            if not os.path.exists(p):
                raise SystemExit(f"Missing file for {name}: {p}")

    log("Parsing dumps ...")
    pg_rows, _ = load_table(*paths["product_group"], table="product_group")
    rp_rows, _ = load_table(*paths["rent_product"], table="rent_product")

    groups = [{dst: coerce(dst, r.get(src)) for src, dst in PRODUCT_GROUP_COLS.items()}
              for r in pg_rows]
    paths_by_group = build_paths(groups)
    log(f"  product_group: {len(paths_by_group)} path(s) built")

    now = store.now()
    products: List[Dict[str, Any]] = []
    skipped_deleted = 0
    for r in rp_rows:
        if not include_deleted and (r.get("is_del") or "0") != "0":
            skipped_deleted += 1
            continue
        row = {dst: coerce(dst, r.get(src)) for src, dst in RENT_PRODUCT_COLS.items()}
        if not row.get("car_number") or row.get("product_id") is None:
            continue
        row["car_number"] = str(row["car_number"]).strip()
        row["plate_valid"] = 1 if PLATE_RE.match(row["car_number"]) else 0
        group_id = row.get("group_id")
        row["model_name"] = paths_by_group.get(group_id) if group_id is not None else None
        row["imported_at"] = now
        products.append(row)

    log(f"  rent_product: {len(products)} live row(s), {skipped_deleted} soft-deleted skipped")
    unnamed = sum(1 for p in products if not p["model_name"])
    if unnamed:
        log(f"  ! {unnamed} row(s) have no product_group match — model_name left empty")

    if dry_run:
        for p in products[:5]:
            log(f"    {p['car_number']} | {p['model_name']} | "
                f"{p['car_manufacture_date']} | {p['mileage']}km | {p['purchase_price']}")
        log("dry-run: nothing written")
        return 0

    store.replace_stock(conn, groups, products)
    log(f"\nImported {len(groups)} product_group row(s) "
        f"and {len(products)} rent_product row(s).")

    counts = store.status_counts(conn, "in_stock = 1 AND plate_valid = 1")
    log("\nStock against Encar:")
    for key, label in (("ON_SALE", "판매중"), ("CHECKING", "확인중"),
                       ("SOLD", "판매완료"), ("NOT_FOUND", "미확인")):
        log(f"  {label:<8} {counts.get(key, 0)}")
    if counts.get("NOT_FOUND"):
        log("\n  미확인 = the plate has never appeared in a crawl. Add these cars' "
            "models to models.json and crawl before reading them as sold.")
    return len(products)
