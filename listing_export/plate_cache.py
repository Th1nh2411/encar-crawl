"""
plate_cache.py -- Remembering what a car id's vehicle number is, between runs.

The vehicle number is the expensive field: the listing endpoint does not carry
it, so every car costs one detail request, and those requests are what run into
Encar's rate limit. A model of 1130 cars is 1130 of them; 16000 cars is 16000.

But a car's vehicle number never changes. Once a car id has been looked up, the
answer is good for as long as the listing exists, so the second run over the
same model only has to ask about ids it has not seen -- typically the handful
that were newly listed. That turns the request count from "every car, every
run" into "new cars only", which is the difference between tripping the rate
limit and never approaching it.

The file is a plain JSON map, id -> {car_number, model_info}:

    {"42688114": {"car_number": "12가3456", "model_info": "BMW 3시리즈 ..."}}

Each entry also carries the model it was collected for, because one file serves
every model: without that, tidying up after a run over one model would delete
every other model's cars. See `prune_to()`.

Nothing here is load-bearing. A missing, unreadable or stale cache only means
more requests, never a wrong answer, so every failure path falls back to
fetching.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple


class PlateCache:
    """id -> {car_number, model_info}, backed by a JSON file.

    `enabled` is False for a cache that was turned off, which lets callers use
    one unconditionally instead of checking for None everywhere.
    """

    def __init__(self, path: Optional[str] = None, enabled: bool = True,
                 model_key: Optional[str] = None,
                 max_age_days: float = 0.0):
        self.path = path
        self.enabled = enabled and bool(path)
        # Which model this run is about. Set once, so the routes do not each
        # have to thread it down to every remember() call.
        self.model_key = model_key
        # How old an entry may be before it is looked up again. The premise of
        # this cache -- a car_id's vehicle number never changes -- has no way
        # to notice when it is wrong: an entry that is bad, for whatever
        # reason, stays bad forever. An age limit is the cheap insurance,
        # because it costs almost nothing in practice: listings rarely outlive
        # a few months, so most entries are replaced by the car being delisted
        # long before they expire. 0 means no limit.
        self.max_age = max_age_days * 86400
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.hits = 0
        self.added = 0
        self.expired = 0
        self._loaded_count = 0

    # ── file ──────────────────────────────────────────────────────────────

    def load(self) -> "PlateCache":
        if not self.enabled or not self.path:
            return self
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # No cache, or a corrupt one. Either way the run just does the
            # requests it would have done anyway.
            return self

        if isinstance(data, dict):
            self.entries = {
                str(k): v for k, v in data.items()
                if isinstance(v, dict) and v.get("car_number")
            }

        # Entries written before there was an age to record are stamped as of
        # now rather than treated as expired. Expiring them would mean looking
        # up every car again on the first run after an upgrade -- 16000
        # requests to enforce a limit that has not been in effect yet. This way
        # the limit starts counting from here, which is what it can honestly
        # claim.
        now = time.time()
        for entry in self.entries.values():
            if not isinstance(entry.get("at"), (int, float)):
                entry["at"] = now

        self._loaded_count = len(self.entries)
        return self

    def save(self) -> Optional[str]:
        """Write the cache back, atomically. Returns the path if it wrote."""
        if not self.enabled or not self.path or not self.added:
            return None
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".",
                        exist_ok=True)
            # Written beside the target and renamed, so a run interrupted
            # mid-write cannot leave a half-file that the next run has to
            # discard.
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.entries, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            return self.path
        except OSError:
            return None

    # ── lookups ───────────────────────────────────────────────────────────

    def split(self, items: Iterable[Dict[str, Any]]
              ) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]],
                         List[Dict[str, Any]]]:
        """Split listing items into (already known, still to fetch).

        The known half comes back paired with its cache entry, so the caller
        can build its rows without another lookup.
        """
        known: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        missing: List[Dict[str, Any]] = []
        cutoff = time.time() - self.max_age if self.max_age else None

        for item in items:
            entry = self.entries.get(str(item.get("Id"))) if self.enabled else None
            if entry and cutoff is not None and entry.get("at", 0) < cutoff:
                # Too old to be trusted. Left in place rather than deleted, so
                # a failed re-fetch does not lose what was already known.
                self.expired += 1
                entry = None
            if entry:
                known.append((item, entry))
            else:
                missing.append(item)

        self.hits += len(known)
        return known, missing

    def remember(self, car_id: Any, car_number: Optional[str],
                 model_info: Optional[str], model_key: Optional[str] = None
                 ) -> None:
        """Record a looked-up car. A car with no number is not worth caching.

        `model_key` is what makes pruning safe: one cache file serves every
        model, so without knowing which model an entry came from, tidying up
        after a run over one model would delete every other model's cars.
        """
        if not self.enabled or not car_number:
            return
        entry = {"car_number": car_number, "model_info": model_info or "",
                 "at": time.time()}
        model_key = model_key or self.model_key
        if model_key:
            entry["model"] = model_key
        self.entries[str(car_id)] = entry
        self.added += 1

    def prune_to(self, live_ids: Iterable[Any], model_key: str) -> int:
        """Drop this model's entries that are not in `live_ids`.

        Only entries tagged with this model are candidates. Anything belonging
        to another model, or written before entries carried a model at all, is
        left alone: an over-large cache costs a few bytes, while deleting a
        live entry costs a request to fetch it again.
        """
        if not self.enabled or not model_key:
            return 0
        keep = {str(i) for i in live_ids}
        dead = [k for k, v in self.entries.items()
                if v.get("model") == model_key and k not in keep]
        for k in dead:
            del self.entries[k]
        if dead:
            self.added += 1  # so save() knows there is something to write
        return len(dead)

    # ── reporting ─────────────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        out = {
            "cacheEnabled": self.enabled,
            "cacheLoaded": self._loaded_count,
            "cacheHits": self.hits,
            "cacheAdded": self.added,
        }
        if self.expired:
            out["cacheExpired"] = self.expired
        return out
