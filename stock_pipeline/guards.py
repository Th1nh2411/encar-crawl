"""
guards.py — The gate that decides whether a crawl is healthy enough to conclude
anything about what is *missing* from it.

A partial crawl must never mark live cars as sold, so every entry point runs the
same check before it touches a status column:

- completion_rate = fetched_pages / total_expected_pages
    >= THRESHOLD_OK (99%)       -> OK, the comparison may run
    >= THRESHOLD_PARTIAL (95%)  -> PARTIAL, comparison skipped
    <  THRESHOLD_PARTIAL        -> FAILED, comparison skipped
- zero-row guard: a batch with 0 rows never runs the comparison
- drop guard: a crawl more than MAX_DROP_RATIO below the previous OK batch for
  the same model skips it too (catches silent blocking / markup changes)

A skipped batch changes no rows; the next healthy run does the work.
"""

from typing import Any, Dict, Optional, Tuple

from config import MAX_DROP_RATIO, THRESHOLD_OK, THRESHOLD_PARTIAL


def evaluate_batch(metrics: Dict[str, Any],
                   previous_count: Optional[int]) -> Tuple[str, bool, str]:
    """-> (batch status, may the comparison run, note)"""
    rate = metrics["completion_rate"]
    count = len(metrics["rows"])

    if metrics["total_expected_pages"] == 0:
        return "FAILED", False, "API reported 0 listings for this model"
    if rate < THRESHOLD_PARTIAL:
        return "FAILED", False, (
            f"completion_rate {rate:.2%} < {THRESHOLD_PARTIAL:.0%} "
            f"({metrics['fetched_pages']}/{metrics['total_expected_pages']} pages)"
        )
    if rate < THRESHOLD_OK:
        return "PARTIAL", False, (
            f"completion_rate {rate:.2%} below the {THRESHOLD_OK:.0%} gate "
            f"({metrics['fetched_pages']}/{metrics['total_expected_pages']} pages)"
        )
    if count == 0:
        return "PARTIAL", False, "zero-row guard: crawl returned no rows"
    if previous_count and count < previous_count * (1 - MAX_DROP_RATIO):
        return "PARTIAL", False, (
            f"drop guard: {count} rows vs {previous_count} in the previous OK batch "
            f"(> {MAX_DROP_RATIO:.0%} drop)"
        )
    return "OK", True, ""
