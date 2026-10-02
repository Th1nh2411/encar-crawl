"""
ladder.py -- Try the routes fastest-first; stop at the first that delivers.

A route "failed" when it raised, returned no rows, filled fewer than
`min_fill` of the car numbers, or lost cars outright because a listing page
never landed -- that last one is checked against the listing's own Count,
because it is otherwise invisible: a run that lost six pages still reports
"1020/1020 car numbers" over the rows that did arrive.

A route that falls short is not thrown away. The fullest one seen is kept, and
if nothing better lands it is used and marked incomplete -- 1020 cars from a
throttled api-direct beats 186 from the collector. Sold detection never runs
on such a run (guards.py), so keeping it costs nothing in correctness.

Blocks are remembered between runs (the `route_state` table): a route that was
throttled is skipped for `cooldown` seconds, because Encar's block is a time
window on this host and asking again inside it holds the window open. A route
refused for account reasons -- a Bright Data zone without KYC (402), a
ScrapingBee plan out of calls -- is parked for a day.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .log import log
from .routes import ROUTES, Crawl, Options, unavailable

THROTTLE_COOLDOWN = 300.0
ZONE_REFUSED_COOLDOWN = 86400.0

THROTTLE_MARKS = ("HTTP 403", "HTTP 407", "HTTP 429")
REFUSED_MARKS = ("HTTP 402", "limit reached")

# A route that comes back with fewer car numbers than this is treated as
# blocked rather than as an answer. 1 page of 20 with 3 numbers is a block.
DEFAULT_MIN_FILL = 0.8

# Missing cars are judged far more strictly than missing numbers, because they
# are invisible. One page of 20 out of 1130 is under 2%, so this tolerates one
# unlucky page and catches the rest.
MISSING_TOLERANCE = 0.02

ALL_ROUTES: Tuple[str, ...] = tuple(ROUTES)

# (name, why it is skipped or None, callable)
Step = Tuple[str, Optional[str], Callable[[], Crawl]]


def looks_throttled(reason: str) -> bool:
    return any(mark in reason for mark in THROTTLE_MARKS)


def worth_racing(reason: str) -> bool:
    """Is this the kind of failure a parallel collector run would help with?

    Racing costs a billed collector run, so it is only worth starting when the
    route failed for a reason the collector is immune to -- being blocked, or
    the network. A misconfiguration fails every route identically.
    """
    if looks_throttled(reason) or any(m in reason for m in REFUSED_MARKS):
        return True
    if any(mark in reason for mark in
           ("Timeout", "ConnectionError", "SSLError", "blocked")):
        return True
    # A route that ran and came back short -- "no rows", "only 600/1130 cars".
    return reason.startswith(("no rows", "only "))


def park(state: Dict[str, Any], name: str, reason: str,
         default_cooldown: float) -> None:
    """Remember that `name` just failed, and for how long that matters."""
    refused = any(mark in reason for mark in REFUSED_MARKS)
    state[name] = {
        "at": time.time(),
        "cooldown": ZONE_REFUSED_COOLDOWN if refused else default_cooldown,
        "why": "account refused (no calls left / no KYC)" if refused else "throttled",
    }


def parked_for(state: Dict[str, Any], name: str,
               default_cooldown: float) -> Optional[str]:
    """How much of `name`'s cooldown is left, as text, or None if it is clear."""
    entry = state.get(name)
    if isinstance(entry, (int, float)):  # written by an older version
        entry = {"at": entry, "cooldown": default_cooldown, "why": "throttled"}
    if not isinstance(entry, dict):
        return None

    at = entry.get("at")
    if not isinstance(at, (int, float)):
        return None
    cooldown = entry.get("cooldown", default_cooldown)
    if not isinstance(cooldown, (int, float)):
        cooldown = default_cooldown

    left = cooldown - (time.time() - at)
    if left <= 0:
        return None
    unit = f"{left / 3600:.0f}h" if left > 3600 else f"{left:.0f}s"
    return f"{entry.get('why', 'failed')} {time.time() - at:.0f}s ago, {unit} left"


def verdict(run: Crawl, min_fill: float) -> Optional[str]:
    """None when the run is good enough to keep; otherwise why it is not."""
    rows = run.get("rows") or []
    if not rows:
        return "no rows"

    expected = run.get("expectedCars") or 0
    if expected and len(rows) < expected * (1 - MISSING_TOLERANCE):
        failed = run.get("pagesFailed") or []
        return (f"only {len(rows)}/{expected} cars"
                + (f", {len(failed)} listing page(s) never landed" if failed else ""))

    filled = sum(1 for r in rows if r.get("vehicle_no"))
    if filled < len(rows) * min_fill:
        return f"only {filled}/{len(rows)} car numbers (< {min_fill:.0%})"
    return None


class BackgroundRoute:
    """A route running on its own thread, to be collected later.

    Only worth doing for the collector, whose time is spent waiting on Bright
    Data rather than on this host. The thread is a daemon: if a cheaper route
    wins, the process exits without waiting -- the collector run finishes on
    Bright Data's side regardless, and is billed either way.
    """

    def __init__(self, name: str, call: Callable[[], Crawl], log=log):
        self.name = name
        self.started = time.time()
        self._value: Optional[Crawl] = None
        self._error: Optional[str] = None
        self._log = log

        def body() -> None:
            try:
                self._value = call()
            except (SystemExit, Exception) as exc:
                self._error = f"{type(exc).__name__}: {exc}".strip()

        self._thread = threading.Thread(target=body, daemon=True)
        self._thread.start()

    def result(self) -> Tuple[Optional[Crawl], Optional[str]]:
        """Wait for it and return (run, reason) the way a direct call does."""
        self._thread.join()
        if self._value is None:
            reason = self._error or "failed"
            self._log(f"[{self.name}] failed after "
                      f"{time.time() - self.started:.1f}s: {reason}")
            return None, reason
        return self._value, None


def start_race(ladder: Sequence[Step], after: int, log=log) -> Optional[BackgroundRoute]:
    """Start the collector in the background if it is still ahead in `ladder`."""
    for name, skip, call in ladder[after + 1:]:
        if name != "collector" or skip:
            continue
        log(f"[{name}] starting in parallel (--race)")
        return BackgroundRoute(name, call, log)
    return None


def build_ladder(names: Sequence[str], spec, pages, known, opts: Options) -> List[Step]:
    return [(name, unavailable(name),
             (lambda fn=ROUTES[name]: fn(spec, pages, known, opts)))
            for name in names]


def run_ladder(ladder: Sequence[Step], state: Dict[str, Any], *,
               min_fill: float = DEFAULT_MIN_FILL,
               cooldown: float = THROTTLE_COOLDOWN,
               race: bool = False) -> Tuple[Optional[Crawl], List[Dict[str, str]]]:
    """Walk the ladder. Returns (the run to use or None, what each route did).

    `state` is the route memory; it is updated in place and the caller saves it.
    """
    attempts: List[Dict[str, str]] = []
    raced: Optional[BackgroundRoute] = None
    best: Optional[Tuple[Crawl, str]] = None

    for index, (name, skip, call) in enumerate(ladder):
        left = None if cooldown <= 0 else parked_for(state, name, cooldown)
        skip = skip or left
        if skip:
            log(f"[{name}] skipped: {skip}")
            attempts.append({"route": name, "result": f"skipped: {skip}"})
            continue

        if raced is not None and raced.name == name:
            log(f"[{name}] waiting on the run started "
                f"{time.time() - raced.started:.0f}s ago")
            candidate, reason = raced.result()
            raced = None
        else:
            log(f"[{name}] trying")
            started = time.time()
            try:
                candidate, reason = call(), None
            # A route fails in whatever way its own layer fails. None of them
            # should end the run while a route is left.
            except (SystemExit, Exception) as exc:
                candidate, reason = None, f"{type(exc).__name__}: {exc}".strip()
                log(f"[{name}] failed after {time.time() - started:.1f}s: {reason}")

        if candidate is not None:
            bad = verdict(candidate, min_fill)
            if bad:
                log(f"[{name}] dropped: {bad}")
                if not best or len(candidate["rows"]) > len(best[0]["rows"]):
                    best = (candidate, bad)
                candidate, reason = None, bad

        if candidate is None:
            attempts.append({"route": name, "result": reason or "failed"})
            if reason and (looks_throttled(reason)
                           or any(m in reason for m in REFUSED_MARKS)):
                park(state, name, reason, cooldown)
            if race and raced is None and worth_racing(reason or ""):
                raced = start_race(ladder, index)
            continue

        state.pop(name, None)
        attempts.append({"route": name, "result": "ok"})
        log(f"[{name}] ok -- {len(candidate['rows'])} rows")
        if raced is not None:
            log(f"  note: the {raced.name} run started in parallel is billed "
                f"whether or not it is used")
            attempts.append({"route": raced.name,
                             "result": "started in parallel, not needed"})
        return candidate, attempts

    if best is not None:
        run, why = best
        run["incomplete"] = why
        log(f"[{run['route']}] using it anyway -- {len(run['rows'])} rows, {why}")
        return run, attempts

    return None, attempts
