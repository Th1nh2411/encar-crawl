"""
plates.py -- Is this plate on sale on Encar right now? A live lookup by plate.

    python main.py check-plate 110모5877 269로1841
    python main.py check-plate --file data/plates.txt --verify

The site's own keyword search, as one listing call per plate:

    (And.Hidden.N._.Simple.keyword(<plate>).)

No live ad for the plate -> SOLD_OUT; ads -> ON_SALE with their ids and prices.
`--verify` asks the detail endpoint about each ad and keeps only the ones whose
plate matches exactly and that are still advertised -- one request per ad.

This is the spot check; the crawl is the systematic one. Each result is shown
next to what the database says about the plate (car_status), so the two can
be compared at a glance. Nothing here writes to the database.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .api import DETAIL_URL, Fetcher, listing_request
from .config import SECTION, WORKERS
from .log import fmt_duration, log
from .models import plate_query

SEARCH_LIMIT = 20

ON_SALE, SOLD_OUT, ERROR = "ON_SALE", "SOLD_OUT", "ERROR"
LABEL = {ON_SALE: "판매중 / on sale", SOLD_OUT: "판매완료 / sold out", ERROR: "ERROR"}

CSV_FIELDS = ("plate", "status", "activeCount", "dbStatus", "carId", "copyType",
              "model", "badge", "price", "url", "note")


def normalize_plate(plate: str) -> str:
    return re.sub(r"[\s\-]", "", plate.strip())


def summarize(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "carId": item.get("Id"),
        "model": " ".join(str(p) for p in (item.get("Manufacturer"), item.get("Model"))
                          if p),
        "badge": item.get("Badge"),
        "formYear": item.get("FormYear"),
        "mileage": item.get("Mileage"),
        "price": item.get("Price"),  # 만원
        # ORIGINAL = the dealer's own ad; DUPLICATION = another dealer reposting it
        "copyType": item.get("ServiceCopyCar"),
        "isOriginal": item.get("ServiceCopyCar") != "DUPLICATION",
        "url": f"https://fem.encar.com/cars/detail/{item.get('Id')}",
    }


def verify(fetcher: Fetcher, plate: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only the ads whose plate matches exactly and that are still advertised."""
    results = fetcher.run([(DETAIL_URL.format(car_id=i.get("Id")),
                            {"include": "ADVERTISEMENT,CATEGORY"}) for i in items])
    kept = []
    for item, res in zip(items, results):
        data = res.data or {}
        if not res.ok:  # 404: the ad was pulled
            continue
        if normalize_plate(data.get("vehicleNo") or "") != normalize_plate(plate):
            continue
        if (data.get("advertisement") or {}).get("status") != "ADVERTISE":
            continue
        kept.append(item)
    return kept


def check_plate(fetcher: Fetcher, plate: str, do_verify: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"plate": plate, "status": ERROR, "activeCount": 0,
                              "note": "", "listings": []}
    res = fetcher.run([listing_request(SECTION, plate_query(plate), 1,
                                       SEARCH_LIMIT, True)])[0]
    if not res.ok:
        result["note"] = f"search failed: {res.error}"
        return result

    items = (res.data or {}).get("SearchResults") or []
    if do_verify and items:
        items = verify(fetcher, plate, items)

    listings = sorted((summarize(i) for i in items), key=lambda l: not l["isOriginal"])
    result.update(listings=listings, activeCount=len(listings),
                  status=ON_SALE if listings else SOLD_OUT,
                  note="" if listings else "no live ad (sold or taken down)")
    return result


def db_status(conn, plate: str) -> Optional[str]:
    if conn is None:
        return None
    row = conn.execute("SELECT status FROM car_status WHERE plate = ?", (plate,)).fetchone()
    return row["status"] if row else "not in database"


def transports() -> List[str]:
    """The API routes' transports this host can use, in ladder order."""
    from .routes import unavailable
    return [kind for kind in ("direct", "scrapingbee", "brightdata")
            if not unavailable(f"api-{kind}")]


def check_plates(plates: List[str], do_verify: bool = False, conn=None,
                 delay: float = 0.0) -> List[Dict[str, Any]]:
    """Check each plate, moving to the next transport when Encar blocks one."""
    kinds = transports()
    fetcher = Fetcher(kinds.pop(0), WORKERS)
    out: List[Dict[str, Any]] = []
    try:
        for idx, plate in enumerate(plates, 1):
            log(f"[{idx}/{len(plates)}] {plate} ...")
            t0 = time.perf_counter()
            res = check_plate(fetcher, plate, do_verify)
            while res["status"] == ERROR and kinds and any(
                    m in res["note"] for m in ("HTTP 401", "HTTP 402", "HTTP 403",
                                               "HTTP 407", "HTTP 429")):
                fetcher.close()
                fetcher = Fetcher(kinds.pop(0), WORKERS)
                log(f"    blocked ({res['note']}) -> switching to {fetcher.name}")
                res = check_plate(fetcher, plate, do_verify)
            res["elapsedSeconds"] = round(time.perf_counter() - t0, 2)
            res["dbStatus"] = db_status(conn, plate)
            out.append(res)

            if res["status"] == ON_SALE:
                log(f"    -> ON SALE ({res['activeCount']} ad(s)) | db: {res['dbStatus']}")
                for l in res["listings"]:
                    tag = "[orig]" if l["isOriginal"] else "[dup] "
                    log(f"       {tag} #{l['carId']} {l['model']} {l['badge'] or ''} | "
                        f"{l['price']}만원 | {l['url']}")
            elif res["status"] == SOLD_OUT:
                log(f"    -> SOLD OUT | db: {res['dbStatus']}")
            else:
                log(f"    -> ERROR: {res['note']}")

            if delay and idx < len(plates):
                time.sleep(delay)
    finally:
        fetcher.close()
    return out


def print_table(rows: List[Dict[str, Any]], elapsed: float) -> None:
    log("\n" + "=" * 86)
    log(f"{'PLATE':<14}{'ENCAR NOW':<24}{'ADS':<6}{'DATABASE':<16}NOTE")
    log("-" * 86)
    for r in rows:
        log(f"{r['plate']:<14}{LABEL[r['status']]:<24}{r['activeCount']:<6}"
            f"{str(r.get('dbStatus') or '-'):<16}{r['note']}")
    log("=" * 86)
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in (ON_SALE, SOLD_OUT, ERROR)}
    log(f"{len(rows)} plate(s) | on sale {counts[ON_SALE]} | sold out {counts[SOLD_OUT]} "
        f"| errors {counts[ERROR]} | {fmt_duration(elapsed)}")


def csv_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flat: one line per ad; a plate with no ad still gets one line."""
    flat: List[Dict[str, Any]] = []
    for r in rows:
        head = {k: r.get(k) for k in ("plate", "status", "activeCount", "dbStatus", "note")}
        if not r["listings"]:
            flat.append(head)
        for l in r["listings"]:
            flat.append({**head, **{k: l[k] for k in ("carId", "copyType", "model",
                                                      "badge", "price", "url")}})
    return flat
