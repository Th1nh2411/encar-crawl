"""
dashboard.py -- Local web UI over the database.

    python main.py dashboard          # http://127.0.0.1:5050  (UI_PORT to change)
    ./serve.sh                        # behind gunicorn, optionally tunnelled

Every page reads the same place. The main page is `car_status` -- one row per
plate, Encar and stock side by side, one status vocabulary -- and everything
else is a closer look at a row of it:

    /                 cars by status: the answer to "is this car still for sale"
    /car/<plate>      one car: its Encar ads, its stock record, its timeline
    /runs             crawl history: which route delivered, what it covered,
                      whether sold detection ran -- plus the tail of the log

    /check            live "is this plate on sale" lookup

Everything is driven from here -- there is no scheduled crawl:

    Crawl now         spawns `main.py crawl` for the chosen model
    Import stock      spawns `main.py import-stock` (the dumps in data/)
    Export CSV/JSON   the cars view as filtered, straight from the database
    Check plate       live lookups, shown next to what the database says

Crawl and import run as a background job, one at a time: this process refuses
a second, and the crawl's lock file blocks one started elsewhere.

Access: with DASHBOARD_PASSWORD set in .env, every request needs an HTTP
Basic login (DASHBOARD_USER, default `admin`). Without a password the
dashboard only answers requests from this machine: another address (ip:port)
or anything through a proxy (Apache, Cloudflare) is refused, so putting it
online without a login fails closed rather than open.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, abort, jsonify, render_template, request

from . import store
from .config import LOG_PATH, ROOT_DIR
from .models import load_specs

app = Flask(__name__)

PAGE_SIZE = 50

DASHBOARD_USER = os.getenv("DASHBOARD_USER") or "admin"
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD") or ""
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP",
                 "CF-Connecting-IP", "Forwarded")


@app.before_request
def require_login():
    if not DASHBOARD_PASSWORD:
        # Without a password only this machine may look. A request from
        # elsewhere either comes from another address (the app bound to
        # 0.0.0.0, reached as ip:port) or through a reverse proxy, which
        # connects from loopback but adds its own headers.
        local = request.remote_addr in ("127.0.0.1", "::1")
        if not local or any(request.headers.get(h) for h in PROXY_HEADERS):
            return Response("Set DASHBOARD_PASSWORD in .env before putting the "
                            "dashboard online.\n", 403, mimetype="text/plain")
        return None
    auth = request.authorization
    if (auth and hmac.compare_digest(auth.username or "", DASHBOARD_USER)
            and hmac.compare_digest(auth.password or "", DASHBOARD_PASSWORD)):
        return None
    return Response("Login required.\n", 401, mimetype="text/plain",
                    headers={"WWW-Authenticate": 'Basic realm="Encar stock tracker"'})

# The one status vocabulary, in the order the page lists them.
STATUS = {
    "ON_SALE":   {"en": "On sale",   "ko": "판매중",   "tone": "ok",
                  "help": "At least one ad is live and was seen in the latest crawl."},
    "CHECKING":  {"en": "Checking",  "ko": "확인중",   "tone": "warn",
                  "help": "Missing from the latest crawl. One more miss and it is sold."},
    "SOLD":      {"en": "Sold",      "ko": "판매완료", "tone": "sold",
                  "help": "Every ad for this plate is gone."},
    "NOT_FOUND": {"en": "Not found", "ko": "미확인",   "tone": "plain",
                  "help": "In stock, never seen on Encar — its model is not crawled yet."},
}
ORDER_SQL = ("CASE status WHEN 'ON_SALE' THEN 0 WHEN 'CHECKING' THEN 1 "
             "WHEN 'SOLD' THEN 2 ELSE 3 END")

# ── Background job (crawl / import) ───────────────────────────────────────────
_lock = threading.Lock()
_current: Dict[str, Any] = {"proc": None, "started_at": None, "label": "", "output": []}


def job_state() -> Dict[str, Any]:
    with _lock:
        proc = _current["proc"]
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "startedAt": _current["started_at"].isoformat(timespec="seconds")
            if _current["started_at"] else None,
            "label": _current["label"],
            "returncode": None if running or proc is None else proc.returncode,
            "output": list(_current["output"])[-60:],
        }


def _pump(proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and "Warning" not in line:
            with _lock:
                _current["output"].append(line)
    proc.wait()


def start_job(label: str, args: List[str]) -> Tuple[bool, str]:
    """Run `main.py <args>` in the background; one job at a time."""
    with _lock:
        proc = _current["proc"]
        if proc is not None and proc.poll() is None:
            return False, f"Busy: {_current['label']} is still running."
        cmd = [sys.executable, "-u", os.path.join(ROOT_DIR, "main.py"), *args]
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT_DIR, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as exc:
            return False, f"Could not start: {exc}"
        _current.update(proc=proc, started_at=datetime.now(), label=label, output=[])
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    return True, f"{label} started."


# ── Data ──────────────────────────────────────────────────────────────────────
def db():
    return store.connect()


def rows_of(sql: str, params=()) -> List[Dict[str, Any]]:
    conn = db()
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def model_options() -> List[Dict[str, str]]:
    try:
        return [{"key": s["key"], "label": s.get("label", s["key"])} for s in load_specs()]
    except SystemExit:
        return []


def page_arg() -> int:
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


def header() -> Dict[str, Any]:
    """What every page shows on top: the last run of each model."""
    last = rows_of(
        "SELECT b.* FROM crawl_batches b JOIN (SELECT model_key, MAX(id) AS id "
        "FROM crawl_batches GROUP BY model_key) m ON m.id = b.id ORDER BY b.id DESC")
    return {"last_runs": last, "models": model_options(), "job": job_state()}


# ── Pages ─────────────────────────────────────────────────────────────────────
def car_filters() -> Tuple[Dict[str, str], List[str], List[Any]]:
    """The cars view's filters from the query string -> (filters, where, params).

    `where` leaves the status out: the page counts every status under the
    other filters, then narrows the table to the chosen one.
    """
    f = {k: (request.args.get(k) or "").strip()
         for k in ("status", "model", "scope", "q", "test")}
    base, params = ["1=1" if f["test"] == "1" else "plate_valid = 1"], []
    if f["model"]:
        base.append("model_key = ?")
        params.append(f["model"])
    if f["scope"] == "stock":
        base.append("in_stock = 1")
    elif f["scope"] == "encar":
        base.append("in_stock = 0")
    if f["q"]:
        base.append("(plate LIKE ? OR model LIKE ?)")
        params += [f"%{f['q']}%", f"%{f['q']}%"]
    return f, base, params


@app.route("/")
def cars():
    page = page_arg()
    filters, base, params = car_filters()
    status = filters["status"]

    # The counts follow every filter but the status one, so the bar shows the
    # make-up of what is being looked at, and each segment is a filter.
    conn = db()
    try:
        counts = store.status_counts(conn, " AND ".join(base), params)
        where = list(base)
        wparams = list(params)
        if status in STATUS:
            where.append("status = ?")
            wparams.append(status)
        clause = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) FROM car_status WHERE {clause}",
                             wparams).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM car_status WHERE {clause} "
            f"ORDER BY {ORDER_SQL}, last_seen_at DESC, plate LIMIT ? OFFSET ?",
            (*wparams, PAGE_SIZE, (page - 1) * PAGE_SIZE))]
        no_plate = conn.execute(
            "SELECT COUNT(*) FROM vehicles WHERE vehicle_no IS NULL OR vehicle_no = ''"
        ).fetchone()[0]
        hidden_test = conn.execute(
            "SELECT COUNT(*) FROM car_status WHERE plate_valid = 0").fetchone()[0]
    finally:
        conn.close()

    return render_template(
        "index.html", view="cars", rows=rows, counts=counts, total=total,
        page=page, pages=max(1, math.ceil(total / PAGE_SIZE)), status_meta=STATUS,
        filters=filters,
        no_plate=no_plate, hidden_test=hidden_test, **header())


@app.route("/car/<plate>")
def car(plate: str):
    conn = db()
    try:
        summary = conn.execute("SELECT * FROM car_status WHERE plate = ?",
                               (plate,)).fetchone()
        if summary is None:
            abort(404)
        ads = [dict(r) for r in conn.execute(
            "SELECT * FROM vehicles WHERE vehicle_no = ? "
            "ORDER BY status, last_seen_at DESC", (plate,))]
        stock = [dict(r) for r in conn.execute(
            "SELECT * FROM rent_product WHERE car_number = ?", (plate,))]
    finally:
        conn.close()
    return render_template("index.html", view="car", car=dict(summary), ads=ads,
                           stock=stock, status_meta=STATUS, filters={}, **header())


@app.route("/runs")
def runs():
    page = page_arg()
    conn = db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM crawl_batches").fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            "SELECT *, CAST((julianday(finished_at) - julianday(started_at)) * 86400 "
            "AS INTEGER) AS duration_s FROM crawl_batches ORDER BY id DESC "
            "LIMIT ? OFFSET ?", (PAGE_SIZE, (page - 1) * PAGE_SIZE))]
        routes = [dict(r) for r in conn.execute("SELECT * FROM route_state ORDER BY route")]
    finally:
        conn.close()
    for r in rows:
        try:
            r["attempts"] = json.loads(r.get("attempts") or "[]")
        except ValueError:
            r["attempts"] = []
    for r in routes:
        r["left"] = max(0, r["cooldown"] - (datetime.now().timestamp() - r["at"]))
    return render_template(
        "index.html", view="runs", rows=rows, total=total, page=page,
        pages=max(1, math.ceil(total / PAGE_SIZE)), routes=routes,
        log_tail=tail_log(), status_meta=STATUS, filters={}, **header())


@app.route("/export.<fmt>")
def export_cars(fmt: str):
    """The cars view, as filtered on the page, as a download."""
    import csv
    import io

    from .export import CAR_FIELDS, envelope

    if fmt not in ("csv", "json"):
        abort(404)
    filters, where, params = car_filters()
    if filters["status"] in STATUS:
        where.append("status = ?")
        params.append(filters["status"])
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {', '.join(CAR_FIELDS)} FROM car_status WHERE {' AND '.join(where)} "
            f"ORDER BY {ORDER_SQL}, plate", params)]
    finally:
        conn.close()

    name = f"cars_{filters['model'] or 'all'}_{datetime.now():%Y%m%d_%H%M%S}"
    if fmt == "json":
        body = json.dumps(envelope("cars", filters["model"] or "all", rows,
                                   {"filters": filters}), ensure_ascii=False, indent=2)
        mimetype = "application/json"
    else:
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM, so Excel opens the Korean text as UTF-8
        writer = csv.DictWriter(buf, fieldnames=list(CAR_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
        body, mimetype = buf.getvalue(), "text/csv"
    return Response(body, mimetype=mimetype, headers={
        "Content-Disposition": f'attachment; filename="{name}.{fmt}"'})


@app.route("/check")
def check_page():
    return render_template("index.html", view="check", status_meta=STATUS,
                           filters={}, **header())


def tail_log(lines: int = 80) -> str:
    if not os.path.exists(LOG_PATH):
        return "(no log yet -- run a crawl)"
    with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
        return "".join(fh.readlines()[-lines:])


# ── API ───────────────────────────────────────────────────────────────────────
def json_body() -> Optional[Dict[str, Any]]:
    """The request's JSON object, or None.

    Actions take JSON only: a form on another site can POST here, but a
    cross-site JSON POST needs a CORS preflight, which this app never grants.
    """
    if not request.is_json:
        return None
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@app.post("/api/crawl")
def api_crawl():
    body = json_body()
    if body is None:
        return jsonify({"ok": False, "message": "JSON body required"}), 415
    models = [m for m in (body.get("models") or []) if m]
    # The body reaches the crawler's argv. It is passed as a list, so no shell
    # sees it, but only keys that exist in models.json belong there.
    known = {m["key"] for m in model_options()}
    unknown = [m for m in models if m not in known]
    if unknown:
        return jsonify({"ok": False, "message": f"Unknown model(s): {', '.join(unknown)}",
                        **job_state()}), 400

    args = ["crawl", "--log-file", LOG_PATH]
    for key in models:
        args += ["--model", key]
    label = "Crawl " + (", ".join(models) or "all models")
    ok, message = start_job(label, args)
    return jsonify({"ok": ok, "message": message, **job_state()}), (200 if ok else 409)


@app.post("/api/import-stock")
def api_import_stock():
    if json_body() is None:
        return jsonify({"ok": False, "message": "JSON body required"}), 415
    ok, message = start_job("Import stock", ["import-stock"])
    return jsonify({"ok": ok, "message": message, **job_state()}), (200 if ok else 409)


@app.get("/api/job")
def api_job():
    return jsonify(job_state())


CHECK_MAX = 50


@app.post("/api/check-plate")
def api_check_plate():
    """Live lookups for up to CHECK_MAX plates, next to the database's view."""
    from .plates import check_plates, normalize_plate

    body = json_body()
    if body is None:
        return jsonify({"ok": False, "message": "JSON body required"}), 415
    raw = body.get("plates") or []
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").splitlines()
    plates = list(dict.fromkeys(p for p in (normalize_plate(str(x)) for x in raw) if p))
    if not plates:
        return jsonify({"ok": False, "message": "No plates given"}), 400
    if len(plates) > CHECK_MAX:
        return jsonify({"ok": False,
                        "message": f"At most {CHECK_MAX} plates at a time"}), 400

    conn = db()
    try:
        results = check_plates(plates, bool(body.get("verify")), conn)
    finally:
        conn.close()
    return jsonify({"ok": True, "results": results})


# ── Template helpers ──────────────────────────────────────────────────────────
@app.template_filter("ago")
def ago(stamp: Optional[str]) -> str:
    """'2026-10-02 09:15:00' -> '3h ago'."""
    if not stamp:
        return "—"
    try:
        then = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(stamp)
    secs = (datetime.now() - then).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


@app.template_filter("num")
def num(value: Any) -> str:
    return "—" if value in (None, "") else f"{int(value):,}"


@app.template_filter("dur")
def dur(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


@app.context_processor
def helpers():
    def url_with(**changes: Any) -> str:
        """The current query string with some keys changed; None drops one."""
        from urllib.parse import urlencode
        args = {k: v for k, v in request.args.items() if v}
        args.pop("page", None)
        for k, v in changes.items():
            if v in (None, ""):
                args.pop(k, None)
            else:
                args[k] = v
        return "?" + urlencode(args) if args else request.path
    return {"url_with": url_with}


def run(port: Optional[int] = None) -> None:
    # Not 5000: macOS AirPlay Receiver binds it and answers 403.
    port = port or int(os.getenv("UI_PORT", "5050"))
    store.connect().close()  # create the database before the first request
    print(f"Encar dashboard: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=bool(os.getenv("UI_DEBUG")))
