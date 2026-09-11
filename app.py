"""
app.py — Local web UI over the Encar crawl database.

An MVP dashboard, not a service: it binds to localhost, has no auth, and runs
the crawler as a subprocess of itself. Do not expose it.

    pip install flask pymysql python-dotenv
    python migrate.py
    python app.py            # http://127.0.0.1:5050  (UI_PORT to change)

Pages
-----
    /            cars, grouped by plate (vehicles_by_plate) — the real answer
                 to "is this car still for sale"
    /listings    raw Encar ads (vehicles) — several per car, price per dealer
    /runs        crawl history from crawl_batches, plus the tail of the log

The Crawl button spawns `encar_list_init.py`. Only one crawl runs at a time:
this process refuses to spawn a second, and the crawler itself takes a MySQL
advisory lock, so a run started from a terminal blocks the button too.
"""

import os
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request

from encar_stock_sync import HERE, connect, load_specs

app = Flask(__name__)

LOG_PATH = os.path.join(HERE, "logs", "encar.log")
PAGE_SIZE = 50

# ── Crawl process state ───────────────────────────────────────────────────────
# One crawl at a time, tracked in memory. The DB lock is the real guard; this
# just gives the UI something to show while the subprocess runs.
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
    """Collect the crawler's output so the UI can show progress live."""
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line or "Warning" in line:
            continue
        with _lock:
            _current["output"].append(line)
    proc.wait()


def start_crawl(models: List[str]) -> Tuple[bool, str]:
    with _lock:
        proc = _current["proc"]
        if proc is not None and proc.poll() is None:
            return False, "A crawl is already running."

        cmd = [sys.executable, os.path.join(HERE, "encar_list_init.py"),
               "--log-file", LOG_PATH]
        for key in models:
            cmd += ["--model", key]

        try:
            proc = subprocess.Popen(
                cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except OSError as exc:
            return False, f"Could not start the crawler: {exc}"

        _current.update(proc=proc, started_at=datetime.now(),
                        models=models or ["(all models)"], output=[])

    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    return True, "Crawl started."


# ── DB helpers ────────────────────────────────────────────────────────────────
def query(sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        import pymysql.cursors
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def scalar(sql: str, params: Tuple = ()) -> Any:
    rows = query(sql, params)
    return list(rows[0].values())[0] if rows else None


def model_options() -> List[Dict[str, str]]:
    try:
        return [{"key": s["key"], "label": s["label"]}
                for s in load_specs(os.path.join(HERE, "models.json"), [])]
    except SystemExit:
        return []


def paging() -> Tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    return page, (page - 1) * PAGE_SIZE


def tail_log(lines: int = 60) -> str:
    if not os.path.exists(LOG_PATH):
        return "(no log file yet — run a crawl)"
    with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-lines:])


# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def cars():
    page, offset = paging()
    status = request.args.get("status", "")
    model = request.args.get("model", "")
    q = (request.args.get("q") or "").strip()

    where, params = ["1=1"], []
    if status in ("ACTIVE", "SOLD"):
        where.append("status = %s")
        params.append(status)
    if model:
        where.append("model_key = %s")
        params.append(model)
    if q:
        where.append("(vehicle_no LIKE %s OR model_info LIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    clause = " AND ".join(where)

    total = scalar(f"SELECT COUNT(*) FROM vehicles_by_plate WHERE {clause}", tuple(params))
    rows = query(
        f"SELECT * FROM vehicles_by_plate WHERE {clause} "
        f"ORDER BY status DESC, last_seen_at DESC LIMIT %s OFFSET %s",
        tuple(params) + (PAGE_SIZE, offset),
    )

    return render_template(
        "index.html", view="cars", rows=rows, page=page, total=total or 0,
        page_size=PAGE_SIZE, models=model_options(), filters={"status": status, "model": model, "q": q},
        stats=car_stats(),
    )


@app.route("/listings")
def listings():
    page, offset = paging()
    status = request.args.get("status", "")
    model = request.args.get("model", "")
    q = (request.args.get("q") or "").strip()

    where, params = ["1=1"], []
    if status in ("ACTIVE", "SOLD"):
        where.append("status = %s")
        params.append(status)
    if model:
        where.append("model_key = %s")
        params.append(model)
    if q:
        where.append("(vehicle_no LIKE %s OR car_id LIKE %s OR model_info LIKE %s)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    clause = " AND ".join(where)

    total = scalar(f"SELECT COUNT(*) FROM vehicles WHERE {clause}", tuple(params))
    rows = query(
        f"SELECT * FROM vehicles WHERE {clause} "
        f"ORDER BY last_seen_at DESC, id DESC LIMIT %s OFFSET %s",
        tuple(params) + (PAGE_SIZE, offset),
    )

    return render_template(
        "index.html", view="listings", rows=rows, page=page, total=total or 0,
        page_size=PAGE_SIZE, models=model_options(),
        filters={"status": status, "model": model, "q": q}, stats=car_stats(),
    )


@app.route("/runs")
def runs():
    page, offset = paging()
    total = scalar("SELECT COUNT(*) FROM crawl_batches")
    rows = query(
        "SELECT *, TIMESTAMPDIFF(SECOND, started_at, finished_at) AS duration_s "
        "FROM crawl_batches ORDER BY id DESC LIMIT %s OFFSET %s",
        (PAGE_SIZE, offset),
    )
    return render_template(
        "index.html", view="runs", rows=rows, page=page, total=total or 0,
        page_size=PAGE_SIZE, models=model_options(), filters={},
        stats=car_stats(), log_tail=tail_log(),
    )


def car_stats() -> Dict[str, Any]:
    """The header numbers. Cars and listings differ — that is the point."""
    try:
        cars_by_status = query(
            "SELECT status, COUNT(*) AS n FROM vehicles_by_plate GROUP BY status")
        listings_total = scalar("SELECT COUNT(*) FROM vehicles") or 0
        last = query(
            "SELECT started_at, finished_at, status FROM crawl_batches "
            "ORDER BY id DESC LIMIT 1")
    except Exception as exc:  # an unmigrated DB must still render the page
        return {"error": f"{type(exc).__name__}: {exc}"}

    by_status = {r["status"]: r["n"] for r in cars_by_status}
    return {
        "cars_active": by_status.get("ACTIVE", 0),
        "cars_sold": by_status.get("SOLD", 0),
        "cars_total": sum(by_status.values()),
        "listings_total": listings_total,
        "last_run": last[0] if last else None,
    }


# ── Crawl API ─────────────────────────────────────────────────────────────────
@app.post("/api/crawl")
def api_crawl():
    models = request.json.get("models") if request.is_json else None
    models = [m for m in (models or []) if m]

    # The request body reaches the crawler's argv. It is passed as a list, so a
    # shell cannot see it, but only keys that actually exist in models.json have
    # any business being there.
    known = {m["key"] for m in model_options()}
    unknown = [m for m in models if m not in known]
    if unknown:
        return jsonify({"ok": False, "message": f"Unknown model(s): {', '.join(unknown)}",
                        **crawl_state()}), 400

    ok, message = start_crawl(models)
    return jsonify({"ok": ok, "message": message, **crawl_state()}), (200 if ok else 409)


@app.get("/api/crawl/status")
def api_crawl_status():
    state = crawl_state()
    state["lastRuns"] = query(
        "SELECT id, model_key, started_at, finished_at, status, crawled_count, "
        "sold_marked_count, note FROM crawl_batches ORDER BY id DESC LIMIT 5"
    )
    return jsonify(state)


@app.template_filter("dt")
def fmt_dt(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "—"


@app.template_filter("dur")
def fmt_dur(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


if __name__ == "__main__":
    # Not 5000: macOS AirPlay Receiver binds it and answers 403, which looks
    # exactly like a stale server still serving an old page.
    port = int(os.getenv("UI_PORT", "5050"))
    print(f"Encar dashboard: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=bool(os.getenv("UI_DEBUG")))
