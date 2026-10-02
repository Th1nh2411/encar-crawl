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

The Crawl button spawns `main.py crawl`. One crawl at a time: this process
refuses a second, and the crawl's lock file blocks one started elsewhere.

Access: with DASHBOARD_PASSWORD set in .env, every request needs HTTP Basic
login (DASHBOARD_USER, default `admin`) -- over the tunnel that is HTTPS.
Without it the dashboard only answers local requests: anything that arrives
through Cloudflare is refused, so a tunnel can never expose it open by mistake.
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


@app.before_request
def require_login():
    if not DASHBOARD_PASSWORD:
        # cloudflared reaches the app on loopback, so the address cannot tell a
        # tunnelled request from a local one; Cloudflare's header can.
        if request.headers.get("CF-Connecting-IP"):
            return Response("Set DASHBOARD_PASSWORD in .env before exposing the "
                            "dashboard.\n", 403, mimetype="text/plain")
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

# ── Crawl subprocess ──────────────────────────────────────────────────────────
_lock = threading.Lock()
_current: Dict[str, Any] = {"proc": None, "started_at": None, "models": [], "output": []}


def crawl_state() -> Dict[str, Any]:
    with _lock:
        proc = _current["proc"]
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "startedAt": _current["started_at"].isoformat(timespec="seconds")
            if _current["started_at"] else None,
            "models": list(_current["models"]),
            "returncode": None if running or proc is None else proc.returncode,
            "output": list(_current["output"])[-40:],
        }


def _pump(proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and "Warning" not in line:
            with _lock:
                _current["output"].append(line)
    proc.wait()


def start_crawl(models: List[str]) -> Tuple[bool, str]:
    with _lock:
        proc = _current["proc"]
        if proc is not None and proc.poll() is None:
            return False, "A crawl is already running."
        cmd = [sys.executable, os.path.join(ROOT_DIR, "main.py"), "crawl",
               "--log-file", LOG_PATH]
        for key in models:
            cmd += ["--model", key]
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT_DIR, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as exc:
            return False, f"Could not start the crawler: {exc}"
        _current.update(proc=proc, started_at=datetime.now(),
                        models=models or ["(all models)"], output=[])
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    return True, "Crawl started."


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
    return {"last_runs": last, "models": model_options(), "crawl": crawl_state()}


# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def cars():
    page = page_arg()
    status = request.args.get("status", "")
    model = request.args.get("model", "")
    scope = request.args.get("scope", "")
    q = (request.args.get("q") or "").strip()
    show_test = request.args.get("test") == "1"

    base, params = ["1=1" if show_test else "plate_valid = 1"], []
    if model:
        base.append("model_key = ?")
        params.append(model)
    if scope == "stock":
        base.append("in_stock = 1")
    elif scope == "encar":
        base.append("in_stock = 0")
    if q:
        base.append("(plate LIKE ? OR model LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]

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
        filters={"status": status, "model": model, "scope": scope, "q": q,
                 "test": "1" if show_test else ""},
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


def tail_log(lines: int = 80) -> str:
    if not os.path.exists(LOG_PATH):
        return "(no log yet -- run a crawl)"
    with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
        return "".join(fh.readlines()[-lines:])


# ── API ───────────────────────────────────────────────────────────────────────
@app.post("/api/crawl")
def api_crawl():
    # JSON only. The browser sends the login with every request, so a form on
    # another site could otherwise start a crawl; a cross-site JSON POST needs
    # a CORS preflight, which this app never grants.
    if not request.is_json:
        return jsonify({"ok": False, "message": "JSON body required"}), 415
    models = (request.get_json(silent=True) or {}).get("models")
    models = [m for m in (models or []) if m]
    # The body reaches the crawler's argv. It is passed as a list, so no shell
    # sees it, but only keys that exist in models.json belong there.
    known = {m["key"] for m in model_options()}
    unknown = [m for m in models if m not in known]
    if unknown:
        return jsonify({"ok": False, "message": f"Unknown model(s): {', '.join(unknown)}",
                        **crawl_state()}), 400
    ok, message = start_crawl(models)
    return jsonify({"ok": ok, "message": message, **crawl_state()}), (200 if ok else 409)


@app.get("/api/crawl/status")
def api_crawl_status():
    return jsonify(crawl_state())


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
