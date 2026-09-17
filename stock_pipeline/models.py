"""
models.py — Reading models.json, the one list of Encar models the whole project
works from. It lives in the project root because listing_export/ reads it too.

Each entry is a `spec`:

    key            short id used on the command line and in `vehicles.model_key`
    label          human name for the logs and the dashboard
    manufacturer / model_group / model    Encar's own query tokens
    car_type       Y for Korean makes, N for imports (default N)
    stock_filter   SQL predicate scoping the `stock` comparison to this model
"""

import json
from typing import Dict, List, Optional

from config import MODELS_FILE


def load_specs(path: Optional[str] = None,
               only: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Every model in models.json, or just the requested keys."""
    with open(path or MODELS_FILE, encoding="utf-8") as f:
        specs = json.load(f)

    if only:
        wanted = set(only)
        specs = [s for s in specs if s["key"] in wanted]
        unknown = wanted - {s["key"] for s in specs}
        if unknown:
            raise SystemExit(f"Unknown model key(s): {', '.join(sorted(unknown))}")

    if not specs:
        raise SystemExit("No models selected")
    return specs
