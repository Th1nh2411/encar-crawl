"""
config.py — Paths and the environment-driven settings the pipeline reads.

Everything here is a value, not behaviour: import it, do not call it. The two
paths are what every other module joins against, and they are the reason this
module exists at all — the scripts live in stock_pipeline/, while models.json,
migrations/, logs/ and the shared .env stay in the project root one level up
(listing_export/ reads the same .env).

    PKG_DIR   this directory — templates/, plates.txt
    ROOT_DIR  the project root — models.json, migrations/, logs/, .env
"""

import os

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)

load_dotenv(os.path.join(ROOT_DIR, os.pardir, ".env"))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

MODELS_FILE = os.path.join(ROOT_DIR, "models.json")
MIGRATIONS_DIR = os.path.join(ROOT_DIR, "migrations")
LOG_PATH = os.path.join(ROOT_DIR, "logs", "encar.log")

# ── Crawl pacing ──────────────────────────────────────────────────────────────
PAGE_SIZE = int(os.getenv("SYNC_PAGE_SIZE", "200"))
LIST_WORKERS = int(os.getenv("SYNC_LIST_WORKERS", "4"))
# Kept deliberately modest: the detail pass is ~1200 requests for one model and
# 8 workers put it around 40 req/s, which is what tripped the CloudFront rate
# limit above. A daily run has no reason to be that fast.
DETAIL_WORKERS = int(os.getenv("SYNC_DETAIL_WORKERS", "4"))
MAX_RETRY = int(os.getenv("SYNC_MAX_RETRY", "4"))
BASE_BACKOFF = float(os.getenv("SYNC_BASE_BACKOFF", "3.0"))
REQUEST_TIMEOUT = float(os.getenv("SYNC_REQUEST_TIMEOUT", "25"))

# Encar sits behind CloudFront and answers a throttled client with 407 (also
# seen as 403/429), Content-Length 0 and `X-Cache: Error from cloudfront`. That
# is a rate limit, not a proxy or session problem: plain curl from the same host
# gets the identical 407. Retrying a few seconds later just burns the attempts,
# so these statuses back off on their own, much slower, scale.
THROTTLE_STATUSES = (403, 407, 429)
THROTTLE_BACKOFF = float(os.getenv("SYNC_THROTTLE_BACKOFF", "45.0"))

# Pause between models. Crawling them back to back trips Encar's CloudFront
# rate limit: after one ~1,200-request model the next one sat in 407 backoff
# for ~5.5 min and only got through on its last retry. A deliberate pause is
# far cheaper than that, and keeps a model from failing outright.
MODEL_COOLDOWN = max(0.0, float(os.getenv("SYNC_MODEL_COOLDOWN", "60")))

# How many consecutive healthy crawls must miss a listing before it counts as
# sold. 1 restores the old mark-on-first-miss behaviour.
SOLD_MISSING_STREAK = max(1, int(os.getenv("SOLD_MISSING_STREAK", "2")))

# ── Safeguard thresholds ──────────────────────────────────────────────────────
# Config, not hardcoded policy — see guards.py.
THRESHOLD_OK = float(os.getenv("SYNC_THRESHOLD_OK", "0.99"))
THRESHOLD_PARTIAL = float(os.getenv("SYNC_THRESHOLD_PARTIAL", "0.95"))
MAX_DROP_RATIO = float(os.getenv("SYNC_MAX_DROP_RATIO", "0.5"))

# ── `stock` table shape ───────────────────────────────────────────────────────
# This repo does not own that schema, so it is configurable.
STOCK_TABLE = os.getenv("STOCK_TABLE", "stock")
STOCK_CAR_ID_COL = os.getenv("STOCK_CAR_ID_COL", "car_id")
STOCK_STATUS_COL = os.getenv("STOCK_STATUS_COL", "status")
STOCK_SOLD_AT_COL = os.getenv("STOCK_SOLD_AT_COL", "sold_confirmed_at")
STOCK_SOLD_VALUE = os.getenv("STOCK_SOLD_VALUE", "SOLD")
