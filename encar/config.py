"""
config.py -- Paths, and the settings read from .env.

Values only: import them, do not call anything here. Every path is anchored on
the project root, so a command behaves the same from any working directory.

    var/encar.db      the database -- the one place data is kept
    var/crawl.lock    held while a crawl runs, so two cannot overlap
    output/           JSON/CSV exports, written only when asked for
    logs/encar.log    the run log the dashboard shows
    data/             the sonokong stock dumps and the sample plate list
"""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; export the variables yourself
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)

load_dotenv(os.path.join(ROOT_DIR, ".env"))


def _path(env: str, default: str) -> str:
    value = os.getenv(env) or default
    return value if os.path.isabs(value) else os.path.join(ROOT_DIR, value)


MODELS_FILE = os.path.join(ROOT_DIR, "models.json")
DATA_DIR = os.path.join(ROOT_DIR, "data")
VAR_DIR = os.path.join(ROOT_DIR, "var")
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
DB_PATH = _path("ENCAR_DB", os.path.join("var", "encar.db"))
LOCK_PATH = os.path.join(VAR_DIR, "crawl.lock")
LOG_PATH = _path("LOG_FILE", os.path.join("logs", "encar.log"))

# The JSON cache the file-only exporter used to keep. Read once into the
# database on the first crawl, so the car numbers it holds are not fetched again.
LEGACY_PLATE_CACHE = os.path.join(VAR_DIR, "plate_cache.json")
LEGACY_THROTTLE_STATE = os.path.join(VAR_DIR, "throttle_state.json")

# ── Listing ───────────────────────────────────────────────────────────────────
# 일반등록. `premium` covers the same cars (identical Count) in another order;
# `general` is the one the search page itself asks for, so every route -- the
# API ones and the browser ones -- pages through the same ordering.
SECTION = "general"

# Cars per listing request on the API routes. The API serves 200 as readily as
# 20, and once the car numbers are known the listing calls are the whole cost
# of a run: 16000 cars is 80 requests this way, 800 at the site's 20.
API_PAGE_SIZE = 200

# The browser routes keep the site's own page size. They do not choose it: ask
# the page for 200 and it quietly serves 20, while the page count is worked
# out from the 200 -- too few pages, cars missing, nothing said.
SITE_PAGE_SIZE = 20

WORKERS = int(os.getenv("ENCAR_WORKERS", "8"))

# Pause between models when a crawl covers several. Back to back, the second
# model starts inside the rate-limit window the first one opened.
MODEL_COOLDOWN = max(0.0, float(os.getenv("ENCAR_MODEL_COOLDOWN", "60")))

# How long a stored car number is trusted before it is looked up again. A
# vehicle number never changes for a listing, so nothing would ever correct a
# bad one; this is the cheap insurance. Most listings are gone long before.
DETAIL_MAX_AGE_DAYS = float(os.getenv("ENCAR_DETAIL_MAX_AGE_DAYS", "90"))

# ── Sold detection ────────────────────────────────────────────────────────────
# Consecutive healthy crawls that must miss a listing before it counts as sold.
SOLD_MISSING_STREAK = max(1, int(os.getenv("SOLD_MISSING_STREAK", "2")))

# The gate in guards.py. A crawl below THRESHOLD_OK never marks anything.
THRESHOLD_OK = float(os.getenv("SOLD_THRESHOLD_OK", "0.99"))
THRESHOLD_PARTIAL = float(os.getenv("SOLD_THRESHOLD_PARTIAL", "0.95"))
MAX_DROP_RATIO = float(os.getenv("SOLD_MAX_DROP_RATIO", "0.5"))
