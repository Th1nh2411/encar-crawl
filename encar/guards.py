"""
guards.py -- Is this crawl healthy enough to conclude anything from what is
missing from it?

Storing a crawl's rows is always safe: the store merges. Marking the cars it
did *not* see is not -- a partial crawl would mark live cars as sold. So sold
detection runs only when every check here passes:

    every page      the run covered the whole model. `--pages 1-5` and the
                    collector (which cannot know the model's size) never do.
    completion      pages fetched / pages requested
                      >= THRESHOLD_OK (99%)        OK
                      >= THRESHOLD_PARTIAL (95%)   PARTIAL, skipped
                      below                        FAILED, skipped
    cars            rows / cars Encar said were there, same threshold -- a page
                    can answer 200 and still come back short
    zero rows       never concludes from nothing
    drop            more than MAX_DROP_RATIO below the previous OK batch for the
                    model: silent blocking or a markup change, not a sell-off

A skipped batch changes no status; the next healthy run does the work.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .config import MAX_DROP_RATIO, THRESHOLD_OK, THRESHOLD_PARTIAL


def evaluate(run: Dict[str, Any], previous_count: Optional[int]) -> Tuple[str, bool, str]:
    """-> (batch status, may sold detection run, note)"""
    rows = len(run.get("rows") or [])
    requested = run.get("pagesRequested") or 0
    fetched = run.get("pagesFetched") or 0
    rate = fetched / requested if requested else 0.0

    if not run.get("everyPage"):
        return "PARTIAL", False, ("not every page was crawled "
                                  "(a --pages subset, or the collector)")
    if not run.get("listingTotal"):
        return "FAILED", False, "Encar reported 0 listings for this model"
    if rate < THRESHOLD_PARTIAL:
        return "FAILED", False, (f"completion {rate:.2%} < {THRESHOLD_PARTIAL:.0%} "
                                 f"({fetched}/{requested} pages)")
    if rate < THRESHOLD_OK:
        return "PARTIAL", False, (f"completion {rate:.2%} below the "
                                  f"{THRESHOLD_OK:.0%} gate ({fetched}/{requested} pages)")
    expected = run.get("expectedCars") or 0
    if expected and rows < expected * THRESHOLD_OK:
        return "PARTIAL", False, (f"{rows}/{expected} cars arrived, below the "
                                  f"{THRESHOLD_OK:.0%} gate")
    if rows == 0:
        return "PARTIAL", False, "zero-row guard: crawl returned no rows"
    if previous_count and rows < previous_count * (1 - MAX_DROP_RATIO):
        return "PARTIAL", False, (f"drop guard: {rows} rows vs {previous_count} in the "
                                  f"previous OK batch (> {MAX_DROP_RATIO:.0%} drop)")
    return "OK", True, ""
