"""
models.py -- models.json, and the search expressions Encar wants for it.

Each entry is a `spec`:

    key            short id used on the command line and in `vehicles.model_key`
    label          human name for the logs and the dashboard
    manufacturer / model_group / model    Encar's own query tokens
    car_type       Y for Korean makes, N for imports (default N)

An entry names as much of the hierarchy as it wants, and the query stops at the
last level given -- so the same file covers one model or a whole manufacturer.
Encar escapes ')' inside names as '_)'; write names that way in models.json.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

from .config import MODELS_FILE

Spec = Dict[str, str]


def load_specs(only: Optional[Sequence[str]] = None,
               path: str = MODELS_FILE) -> List[Spec]:
    """Every model in models.json, or just the requested keys, in file order."""
    try:
        with open(path, encoding="utf-8") as fh:
            specs = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"models.json not found at {path}")

    if only:
        wanted = list(dict.fromkeys(only))
        by_key = {s.get("key"): s for s in specs}
        unknown = [k for k in wanted if k not in by_key]
        if unknown:
            known = ", ".join(s.get("key", "?") for s in specs)
            raise SystemExit(f"Unknown model {', '.join(unknown)}. "
                             f"models.json has: {known}")
        specs = [by_key[k] for k in wanted]

    if not specs:
        raise SystemExit("No models selected")
    return specs


def model_query(spec: Spec) -> str:
    """Encar's search expression for a spec, at whatever depth it gives.

    `Hidden.N` restricts it to listings currently advertised. The expression
    nests one level per field and stops at the last one the spec fills in:

        model  (And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._.
               (C.ModelGroup.3시리즈._.Model.3시리즈 (G20_).))))
        group  (And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._.
               ModelGroup.3시리즈.)))
        make   (And.Hidden.N._.(C.CarType.N._.Manufacturer.벤츠.))

    Note the shape change at the innermost level: a field with a child below it
    is `(C.Field.value._.<child>)`, the last one is just `Field.value.` -- which
    is why this is built rather than formatted.
    """
    levels: List[Tuple[str, str]] = [("CarType", spec.get("car_type") or "N")]
    for field, key in (("Manufacturer", "manufacturer"),
                       ("ModelGroup", "model_group"),
                       ("Model", "model")):
        value = spec.get(key)
        if not value:
            break  # a gap ends the chain: no Model without its ModelGroup
        levels.append((field, value))

    def nest(rest: List[Tuple[str, str]]) -> str:
        field, value = rest[0]
        if len(rest) == 1:
            return f"{field}.{value}."
        return f"(C.{field}.{value}._.{nest(rest[1:])})"

    return f"(And.Hidden.N._.{nest(levels)})"


def plate_query(plate: str) -> str:
    """Search expression for one vehicle number, on sale now.

    No CarType: the stock this is asked about is mostly Korean-market cars,
    and `CarType.N` (imports only) would report every one of them sold.
    Verified against the live API to find the same listings either way for an
    imported car.
    """
    return f"(And.Hidden.N._.Simple.keyword({plate}).)"
