"""
check_plate_status.py — Kiểm tra trạng thái "Sold Out" của xe trên Encar theo
BIỂN SỐ (không đụng tới database).

Cách hoạt động
--------------
Trang https://www.encar.com/fc/fc_carsearchlist.do#!{...Simple.keyword(110모5877)...}
thực chất gọi listing API với query theo biển số (xem encar_api.plate_query):

    (And.Hidden.N._.CarType.N._.Simple.keyword(<biển số>).)

- `Hidden.N`  = tin đang được đăng bán (đang hiển thị).
- Không còn tin nào cho biển số đó  -> xe đã bán / đã gỡ tin  => SOLD_OUT.
- Còn tin  -> ON_SALE (kèm carId, giá, số km...).

Tuỳ chọn `--verify` gọi thêm detail API để đối chiếu chính xác biển số và
trạng thái quảng cáo (`advertisement.status == "ADVERTISE"`, 404 nghĩa là tin
đã bị gỡ/bán). Chậm hơn nhiều vì tốn 1 request cho mỗi tin.

Retry/backoff dùng chung với cả pipeline (encar_api.py): Encar chặn 407 khi bắn
quá nhanh, nên script chạy tuần tự theo từng biển số.

Usage
-----
    # Truyền trực tiếp
    python stock_pipeline/check_plate_status.py 110모5877 269로1841

    # Đọc từ file (mỗi dòng 1 biển số)
    python stock_pipeline/check_plate_status.py --file stock_pipeline/plates.txt

    # Xuất JSON + CSV theo tên tự đặt, hoặc để --out-dir tự đặt tên theo giờ chạy
    python stock_pipeline/check_plate_status.py --file plates.txt --json out.json --csv out.csv
    python stock_pipeline/check_plate_status.py --file plates.txt --out-dir

    # Đối chiếu chính xác biển số bằng detail API (chậm hơn)
    python stock_pipeline/check_plate_status.py 110모5877 --verify
"""

import argparse
import os
import random
import re
import sys
import time
from typing import Any, Dict, List

import results
from config import ROOT_DIR
from encar_api import fetch_listing_page, fetch_vehicle_detail, plate_query
from run_log import fmt_duration, log

TOOL = "check_plate_status"
DEFAULT_OUT_DIR = os.path.join(ROOT_DIR, "output", TOOL)

# Đo thực tế: 80 request liên tục qua requests.Session (keep-alive + cookie),
# delay = 0 -> 0 lỗi, ~3.3 req/s. Chặn 407 chủ yếu xảy ra khi mỗi request mở
# kết nối mới (vd: curl rời rạc), nên giữ nguyên Session quan trọng hơn delay.
DEFAULT_DELAY = 0.0
SEARCH_LIMIT = 20
DETAIL_PAUSE = 1.0  # giữa 2 detail khi --verify

STATUS_ON_SALE = "ON_SALE"
STATUS_SOLD_OUT = "SOLD_OUT"
STATUS_ERROR = "ERROR"

STATUS_LABEL = {
    STATUS_ON_SALE: "판매중 / On sale",
    STATUS_SOLD_OUT: "판매완료 / SOLD OUT",
    STATUS_ERROR: "ERROR",
}


def normalize_plate(plate: str) -> str:
    """Bỏ khoảng trắng / dấu gạch thừa trong biển số."""
    return re.sub(r"[\s\-]", "", plate.strip())


# ── Core ──────────────────────────────────────────────────────────────────────
def summarize_listing(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "carId": item.get("Id"),
        "manufacturer": item.get("Manufacturer"),
        "model": item.get("Model"),
        "badge": item.get("Badge"),
        "formYear": item.get("FormYear"),
        "mileage": item.get("Mileage"),
        "price": item.get("Price"),          # đơn vị: 만원
        "sellType": item.get("SellType"),
        # ORIGINAL = tin gốc, DUPLICATION = tin dealer đăng trùng (xài lại ảnh tin gốc)
        "copyType": item.get("ServiceCopyCar"),
        "isOriginal": item.get("ServiceCopyCar") != "DUPLICATION",
        "officeName": item.get("OfficeName"),
        "modifiedDate": item.get("ModifiedDate"),
        "url": f"https://fem.encar.com/cars/detail/{item.get('Id')}",
    }


def verify_listings(plate: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Giữ lại tin có biển số khớp *chính xác* và đang thật sự quảng cáo."""
    verified: List[Dict[str, Any]] = []
    for item in items:
        detail = fetch_vehicle_detail(str(item.get("Id")))
        time.sleep(DETAIL_PAUSE)
        if not detail:  # 404 -> tin đã gỡ
            continue
        if normalize_plate(detail.get("vehicleNo") or "") != normalize_plate(plate):
            continue
        if (detail.get("advertisement") or {}).get("status") != "ADVERTISE":
            continue
        verified.append(item)
    return verified


def check_plate(plate: str, verify: bool = False) -> Dict[str, Any]:
    """Trạng thái sold-out của 1 biển số."""
    result: Dict[str, Any] = {
        "plate": plate,
        "status": STATUS_ERROR,
        "soldOut": None,
        "activeCount": 0,
        "originalCarId": None,
        "note": "",
        "listings": [],
    }

    try:
        data = fetch_listing_page(plate_query(plate), 0, SEARCH_LIMIT, want_count=True)
    except RuntimeError as exc:
        result["note"] = f"search failed: {exc}"
        return result

    items = data.get("SearchResults") or []
    if verify and items:
        items = verify_listings(plate, items)

    listings = [summarize_listing(i) for i in items]
    # Tin gốc lên trước, tin trùng xuống sau.
    listings.sort(key=lambda l: not l["isOriginal"])

    result["activeCount"] = len(listings)
    result["listings"] = listings
    result["originalCarId"] = next((l["carId"] for l in listings if l["isOriginal"]), None)

    if listings:
        result["status"] = STATUS_ON_SALE
        result["soldOut"] = False
    else:
        result["status"] = STATUS_SOLD_OUT
        result["soldOut"] = True
        result["note"] = "Không còn tin đăng đang bán (đã bán hoặc đã gỡ tin)"

    return result


def check_plates(plates: List[str], verify: bool = False,
                 delay: float = DEFAULT_DELAY) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    total = len(plates)

    for idx, plate in enumerate(plates, 1):
        log(f"[{idx}/{total}] {plate} ...")
        t0 = time.perf_counter()
        res = check_plate(plate, verify=verify)
        # Thời gian gọi API cho biển này (không tính delay chờ giữa các biển).
        res["elapsedSeconds"] = round(time.perf_counter() - t0, 2)
        out.append(res)

        if res["status"] == STATUS_ON_SALE:
            log(f"    -> ON SALE ({res['activeCount']} tin) [{res['elapsedSeconds']}s]")
            for l in res["listings"]:
                tag = "[GỐC] " if l["isOriginal"] else "[TRÙNG]"
                log(f"       {tag} #{l['carId']} {l['manufacturer']} {l['model']} "
                    f"{l['badge']} | {l['price']}만원 | {l['url']}")
        elif res["status"] == STATUS_SOLD_OUT:
            log(f"    -> SOLD OUT [{res['elapsedSeconds']}s]")
        else:
            log(f"    -> ERROR: {res['note']} [{res['elapsedSeconds']}s]")

        if idx < total and delay:
            # Jitter tỉ lệ theo delay, để --delay 0 nghĩa là chạy hết tốc lực.
            time.sleep(delay + random.uniform(0, delay * 0.4))

    return out


# ── Output ────────────────────────────────────────────────────────────────────
def print_table(rows: List[Dict[str, Any]], total_elapsed: float) -> None:
    log("")
    log("=" * 80)
    log(f"{'BIỂN SỐ':<14}{'TRẠNG THÁI':<24}{'SỐ TIN':<8}{'THỜI GIAN':<11}{'GHI CHÚ'}")
    log("-" * 80)
    for r in rows:
        took = f"{r.get('elapsedSeconds', 0):.2f}s"
        log(f"{r['plate']:<14}{STATUS_LABEL[r['status']]:<24}{r['activeCount']:<8}"
            f"{took:<11}{r['note']}")
    log("=" * 80)

    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in (STATUS_ON_SALE, STATUS_SOLD_OUT, STATUS_ERROR)}
    log(f"Tổng: {len(rows)} | Đang bán: {counts[STATUS_ON_SALE]} | "
        f"Sold out: {counts[STATUS_SOLD_OUT]} | Lỗi: {counts[STATUS_ERROR]}")

    api_time = sum(r.get("elapsedSeconds", 0) for r in rows)
    avg = api_time / len(rows) if rows else 0.0
    log(f"Tổng thời gian chạy : {fmt_duration(total_elapsed)}")
    log(f"  - Gọi API         : {fmt_duration(api_time)} (TB {avg:.2f}s/biển)")
    log(f"  - Chờ/delay       : {fmt_duration(max(total_elapsed - api_time, 0))}")


def csv_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """CSV phẳng: 1 dòng cho mỗi tin đăng, biển không có tin vẫn giữ 1 dòng."""
    flat: List[Dict[str, Any]] = []
    for r in rows:
        head = {k: r[k] for k in ("plate", "status", "soldOut", "activeCount")}
        head["elapsedSeconds"] = r.get("elapsedSeconds", "")
        head["note"] = r["note"]
        if not r["listings"]:
            flat.append(head)
            continue
        for l in r["listings"]:
            flat.append({**head, "carId": l["carId"], "copyType": l["copyType"],
                         "model": l["model"], "badge": l["badge"],
                         "price": l["price"], "url": l["url"]})
    return flat


CSV_FIELDS = ("plate", "status", "soldOut", "activeCount", "elapsedSeconds",
              "carId", "copyType", "model", "badge", "price", "url", "note")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kiểm tra trạng thái Sold Out của xe trên Encar theo biển số."
    )
    parser.add_argument("plates", nargs="*", help="Danh sách biển số (vd: 110모5877 269로1841)")
    parser.add_argument("--file", help="File chứa biển số, mỗi dòng 1 biển")
    parser.add_argument("--verify", action="store_true",
                        help="Đối chiếu biển số bằng detail API (chính xác hơn, chậm hơn)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Delay giữa 2 biển số (mặc định {DEFAULT_DELAY}s)")
    parser.add_argument("--json", dest="json_out", help="Đường dẫn file JSON output")
    parser.add_argument("--csv", dest="csv_out", help="Đường dẫn file CSV output")
    parser.add_argument("--out-dir", nargs="?", const=DEFAULT_OUT_DIR,
                        help=f"Ghi cả JSON + CSV, tên <plates>_<timestamp> trong thư mục này "
                             f"(mặc định: {os.path.relpath(DEFAULT_OUT_DIR, ROOT_DIR)})")
    args = parser.parse_args()

    raw: List[str] = list(args.plates)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            raw.extend(line for line in f.read().splitlines() if line.strip())

    if not raw:
        parser.print_help()
        return 1

    # Loại trùng, giữ thứ tự.
    seen = set()
    plates: List[str] = []
    for p in raw:
        n = normalize_plate(p)
        if n and n not in seen:
            seen.add(n)
            plates.append(n)

    started = time.perf_counter()
    rows = check_plates(plates, verify=args.verify, delay=args.delay)
    total_elapsed = time.perf_counter() - started

    print_table(rows, total_elapsed)

    if args.json_out or args.csv_out or args.out_dir:
        meta = {
            "totalPlates": len(rows),
            "verified": args.verify,
            "elapsedSeconds": round(total_elapsed, 2),
            "apiElapsedSeconds": round(
                sum(r.get("elapsedSeconds", 0) for r in rows), 2),
        }
        # JSON giữ nguyên cấu trúc lồng (có listings); CSV thì phẳng.
        for path in results.save(
            TOOL, "plates", rows, meta,
            json_path=args.json_out, csv_path=args.csv_out, out_dir=args.out_dir,
            fields=CSV_FIELDS, flat_rows=csv_rows(rows),
        ):
            log(f"Đã ghi: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
