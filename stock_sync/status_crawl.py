"""
status_crawl.py — Kiểm tra trạng thái "Sold Out" của xe trên Encar theo BIỂN SỐ.

Cách hoạt động
--------------
Trang https://www.encar.com/fc/fc_carsearchlist.do#!{...Simple.keyword(110모5877)...}
thực chất gọi API:

    GET https://api.encar.com/search/car/list/premium
        ?count=true
        &q=(And.Hidden.N._.CarType.N._.Simple.keyword(<biển số>).)
        &sr=|ModifiedDate|<offset>|<limit>

- `Hidden.N`  = tin đang được đăng bán (đang hiển thị).
- Nếu không còn tin nào cho biển số đó  -> xe đã bán / đã gỡ tin  => SOLD_OUT.
- Nếu còn tin  -> ON_SALE (kèm carId, giá, số km...).

Tuỳ chọn `--verify` sẽ gọi thêm detail API để đối chiếu chính xác biển số
và trạng thái quảng cáo:

    GET https://api.encar.com/v1/readside/vehicle/<carId>
        -> advertisement.status == "ADVERTISE"  (404 nghĩa là tin đã bị gỡ/bán)

Lưu ý: Encar chặn khá gắt (HTTP 407) nếu bắn request quá nhanh, nên script
chạy tuần tự có delay + exponential backoff.

Usage
-----
    pip install requests

    # Truyền trực tiếp
    python status_crawl.py 110모5877 269로1841 248부5916

    # Đọc từ file (mỗi dòng 1 biển số)
    python status_crawl.py --file plates.txt

    # Xuất JSON / CSV
    python status_crawl.py --file plates.txt --json out.json --csv out.csv

    # Đối chiếu chính xác biển số bằng detail API (chậm hơn)
    python status_crawl.py 110모5877 --verify
"""

import argparse
import csv
import json
import random
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_URL = "https://api.encar.com/search/car/list/premium"
DETAIL_URL = "https://api.encar.com/v1/readside/vehicle/{car_id}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.encar.com/fc/fc_carsearchlist.do?carType=for",
    "Origin": "https://www.encar.com",
}

MAX_RETRY = 6
BASE_BACKOFF = 5.0      # giây, nhân dần theo số lần retry

# Đo thực tế: 80 request liên tục qua requests.Session (keep-alive + cookie),
# delay = 0 -> 0 lỗi, ~3.3 req/s. 0.5s là mức an toàn dư cho list dài.
# Chặn 407 chủ yếu xảy ra khi mỗi request mở kết nối mới (vd: curl rời rạc),
# nên giữ nguyên Session quan trọng hơn là tăng delay.
DELAY_BETWEEN = 0     # giây nghỉ giữa 2 biển số

STATUS_ON_SALE = "ON_SALE"
STATUS_SOLD_OUT = "SOLD_OUT"
STATUS_ERROR = "ERROR"


def log(msg: str) -> None:
    print(msg, flush=True)


def fmt_duration(seconds: float) -> str:
    """3.4  ->  '3.40s'   |   135.2  ->  '2m 15.20s'"""
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:.2f}s"


def normalize_plate(plate: str) -> str:
    """Bỏ khoảng trắng / dấu gạch thừa trong biển số."""
    return re.sub(r"[\s\-]", "", plate.strip())


# ── HTTP ──────────────────────────────────────────────────────────────────────
def request_json(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, str]] = None,
) -> Any:
    """GET trả JSON, có retry/backoff cho 407/429/5xx. 404 -> None."""
    last_err: Optional[str] = None

    for attempt in range(MAX_RETRY):
        try:
            resp = session.get(url, params=params, timeout=25)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                if not resp.content:
                    return None
                return resp.json()
            if resp.status_code == 404:
                return None
            last_err = f"HTTP {resp.status_code}"

        if attempt < MAX_RETRY - 1:
            wait = BASE_BACKOFF * (attempt + 1) + random.uniform(0, 2)
            log(f"    ... {last_err}, retry sau {wait:.1f}s")
            time.sleep(wait)

    raise RuntimeError(last_err or "unknown error")


def search_by_plate(session: requests.Session, plate: str, hidden: str = "N") -> Dict[str, Any]:
    """Tìm tin đăng theo biển số. hidden='N': đang bán, 'Y': tin ẩn."""
    params = {
        "count": "true",
        "q": f"(And.Hidden.{hidden}._.CarType.N._.Simple.keyword({plate}).)",
        "sr": "|ModifiedDate|0|20",
    }
    data = request_json(session, SEARCH_URL, params)
    if not data:
        return {"Count": 0, "SearchResults": []}
    return data


def fetch_detail(session: requests.Session, car_id: str) -> Optional[Dict[str, Any]]:
    """Detail của 1 tin. None nếu tin đã bị gỡ (404)."""
    return request_json(session, DETAIL_URL.format(car_id=car_id))


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


def check_plate(session: requests.Session, plate: str, verify: bool = False) -> Dict[str, Any]:
    """Trả về trạng thái sold-out của 1 biển số."""
    result: Dict[str, Any] = {
        "plate": plate,
        "status": STATUS_ERROR,
        "soldOut": None,
        "activeCount": 0,
        "listings": [],
        "note": "",
    }

    try:
        data = search_by_plate(session, plate, hidden="N")
    except RuntimeError as exc:
        result["note"] = f"search failed: {exc}"
        return result

    items = data.get("SearchResults") or []

    if verify and items:
        # Đối chiếu biển số thật trong detail API (search keyword có thể khớp mờ).
        verified: List[Dict[str, Any]] = []
        for item in items:
            car_id = str(item.get("Id"))
            try:
                detail = fetch_detail(session, car_id)
            except RuntimeError as exc:
                log(f"    ! detail {car_id} lỗi: {exc}")
                continue
            time.sleep(1.0)
            if not detail:
                continue  # 404 -> tin đã gỡ
            if normalize_plate(detail.get("vehicleNo") or "") != normalize_plate(plate):
                continue
            if (detail.get("advertisement") or {}).get("status") != "ADVERTISE":
                continue
            verified.append(item)
        items = verified

    listings = [summarize_listing(i) for i in items]
    # Tin gốc lên trước, tin trùng xuống sau.
    listings.sort(key=lambda l: not l["isOriginal"])

    result["activeCount"] = len(listings)
    result["listings"] = listings
    result["originalCarId"] = next((l["carId"] for l in listings if l["isOriginal"]), None)

    if items:
        result["status"] = STATUS_ON_SALE
        result["soldOut"] = False
    else:
        result["status"] = STATUS_SOLD_OUT
        result["soldOut"] = True
        result["note"] = "Không còn tin đăng đang bán (đã bán hoặc đã gỡ tin)"

    return result


def run(plates: List[str], verify: bool = False, delay: float = DELAY_BETWEEN) -> List[Dict[str, Any]]:
    session = requests.Session()
    session.headers.update(HEADERS)

    # Lấy cookie từ trang chủ, giảm khả năng bị chặn 407.
    try:
        session.get("https://www.encar.com/", timeout=20)
    except requests.RequestException:
        pass

    results: List[Dict[str, Any]] = []
    total = len(plates)

    for idx, raw in enumerate(plates, 1):
        plate = normalize_plate(raw)
        if not plate:
            continue
        log(f"[{idx}/{total}] {plate} ...")
        t0 = time.perf_counter()
        res = check_plate(session, plate, verify=verify)
        # Thời gian thực sự gọi API cho biển này (không tính delay chờ giữa các biển).
        res["elapsedSeconds"] = round(time.perf_counter() - t0, 2)
        results.append(res)

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

        if idx < total:
            # Jitter tỉ lệ theo delay, để --delay 0 nghĩa là chạy hết tốc lực.
            time.sleep(delay + random.uniform(0, delay * 0.4))

    return results


# ── Output ────────────────────────────────────────────────────────────────────
def print_table(results: List[Dict[str, Any]], total_elapsed: float) -> None:
    log("")
    log("=" * 80)
    log(f"{'BIỂN SỐ':<14}{'TRẠNG THÁI':<24}{'SỐ TIN':<8}{'THỜI GIAN':<11}{'GHI CHÚ'}")
    log("-" * 80)
    for r in results:
        label = {
            STATUS_ON_SALE: "판매중 / On sale",
            STATUS_SOLD_OUT: "판매완료 / SOLD OUT",
            STATUS_ERROR: "ERROR",
        }[r["status"]]
        took = f"{r.get('elapsedSeconds', 0):.2f}s"
        log(f"{r['plate']:<14}{label:<24}{r['activeCount']:<8}{took:<11}{r['note']}")
    log("=" * 80)

    sold = sum(1 for r in results if r["status"] == STATUS_SOLD_OUT)
    onsale = sum(1 for r in results if r["status"] == STATUS_ON_SALE)
    err = sum(1 for r in results if r["status"] == STATUS_ERROR)
    log(f"Tổng: {len(results)} | Đang bán: {onsale} | Sold out: {sold} | Lỗi: {err}")

    api_time = sum(r.get("elapsedSeconds", 0) for r in results)
    avg = api_time / len(results) if results else 0.0
    log(f"Tổng thời gian chạy : {fmt_duration(total_elapsed)}")
    log(f"  - Gọi API         : {fmt_duration(api_time)} (TB {avg:.2f}s/biển)")
    log(f"  - Chờ/delay       : {fmt_duration(max(total_elapsed - api_time, 0))}")


def write_json(path: str, results: List[Dict[str, Any]], total_elapsed: float) -> None:
    payload = {
        "crawledAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "totalPlates": len(results),
        "totalElapsedSeconds": round(total_elapsed, 2),
        "apiElapsedSeconds": round(sum(r.get("elapsedSeconds", 0) for r in results), 2),
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"Đã ghi JSON: {path}")


def write_csv(path: str, results: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["plate", "status", "soldOut", "activeCount", "elapsedSeconds",
                    "carId", "copyType", "model", "badge", "price", "url", "note"])
        for r in results:
            head = [r["plate"], r["status"], r["soldOut"], r["activeCount"],
                    r.get("elapsedSeconds", "")]
            if r["listings"]:
                for l in r["listings"]:
                    w.writerow(head + [l["carId"], l["copyType"], l["model"], l["badge"],
                                       l["price"], l["url"], r["note"]])
            else:
                w.writerow(head + ["", "", "", "", "", "", r["note"]])
    log(f"Đã ghi CSV: {path}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kiểm tra trạng thái Sold Out của xe trên Encar theo biển số."
    )
    parser.add_argument("plates", nargs="*", help="Danh sách biển số (vd: 110모5877 269로1841)")
    parser.add_argument("--file", help="File chứa biển số, mỗi dòng 1 biển")
    parser.add_argument("--verify", action="store_true",
                        help="Đối chiếu biển số bằng detail API (chính xác hơn, chậm hơn)")
    parser.add_argument("--delay", type=float, default=DELAY_BETWEEN,
                        help=f"Delay giữa các request (mặc định {DELAY_BETWEEN}s)")
    parser.add_argument("--json", dest="json_out", help="Đường dẫn file JSON output")
    parser.add_argument("--csv", dest="csv_out", help="Đường dẫn file CSV output")
    args = parser.parse_args()

    plates: List[str] = list(args.plates)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            plates.extend(line for line in f.read().splitlines() if line.strip())

    if not plates:
        parser.print_help()
        return 1

    # Loại trùng, giữ thứ tự.
    seen = set()
    ordered: List[str] = []
    for p in plates:
        n = normalize_plate(p)
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)

    started = time.perf_counter()
    results = run(ordered, verify=args.verify, delay=args.delay)
    total_elapsed = time.perf_counter() - started

    print_table(results, total_elapsed)

    if args.json_out:
        write_json(args.json_out, results, total_elapsed)
    if args.csv_out:
        write_csv(args.csv_out, results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
