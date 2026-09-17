#!/usr/bin/env python3
"""
test_crawl_logic.py -- The decisions the crawl makes, tested without a network.

    python tests/test_crawl_logic.py

Everything here is logic that used to be checked by running a real export and
reading the log, which costs minutes and spends the rate-limit budget that the
next run needs. These are the parts worth pinning down, each one written
because it was wrong at some point:

  verdict       a run that lost six listing pages still reported "1020/1020
                car numbers" and called itself ok, because every count was
                taken over the rows that arrived rather than the rows asked for.
  auto_delay    pacing keyed on a guess about `--pages all` slowed a 33s run
                down to 25 minutes.
  model_query   the expression changes shape at its innermost level, so it is
                built rather than formatted, and the built one has to still
                match the string the site itself produces.
  PlateCache    a cache that loses or invents an entry is worse than no cache.

Plain asserts and a main(), so it runs anywhere python does -- no pytest, no
fixtures, nothing to install.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "listing_export"))

import main  # noqa: E402
from export_listings import (  # noqa: E402
    DETAIL_CHUNK, LISTING_CHUNK, RATE_BUDGET, auto_delay, model_query, paced,
)
from plate_cache import PlateCache  # noqa: E402


def rows(count: int, with_number: bool = True):
    return [{"car_id": str(i), "car_number": f"plate{i}" if with_number else None}
            for i in range(count)]


# ── verdict: is this run good enough to keep? ─────────────────────────────────

def test_verdict_accepts_a_complete_run():
    run = {"rows": rows(1130), "expectedCars": 1130, "pagesFailed": []}
    assert main.verdict(run, 0.8) is None


def test_verdict_rejects_missing_cars_even_when_every_row_is_filled():
    """The regression this whole check exists for.

    1020 rows, every one with a car number, so the old rule saw 1020/1020 and
    was happy. 110 cars were missing and nothing said so.
    """
    run = {"rows": rows(1020), "expectedCars": 1130,
           "pagesFailed": [48, 50, 54, 55, 56, 57]}
    why = main.verdict(run, 0.8)
    assert why and "1020/1130" in why, why
    assert "6 listing page(s)" in why, why


def test_verdict_tolerates_one_unlucky_page():
    """A single page out of 57 is under the 2% tolerance -- not worth falling
    through to a slower route for."""
    run = {"rows": rows(1110), "expectedCars": 1130, "pagesFailed": [57]}
    assert main.verdict(run, 0.8) is None


def test_verdict_rejects_empty_car_numbers():
    run = {"rows": rows(20, with_number=False) + rows(5), "expectedCars": 25}
    why = main.verdict(run, 0.8)
    assert why and "car numbers" in why, why


def test_verdict_rejects_nothing_at_all():
    assert main.verdict({"rows": []}, 0.8) == "no rows"


def test_verdict_without_an_expected_count():
    """The collector cannot report what it did not fetch, so completeness is
    simply not checked for it -- it must not be rejected for that."""
    assert main.verdict({"rows": rows(186), "partial": True}, 0.8) is None


# ── auto_delay: how hard to brake ────────────────────────────────────────────

def test_no_delay_below_the_budget():
    assert auto_delay(21, DETAIL_CHUNK) == 0.0            # one page
    assert auto_delay(1197, DETAIL_CHUNK) == 0.0          # 1130 cars, measured ok


def test_delay_above_the_budget():
    assert auto_delay(16800, DETAIL_CHUNK) > 0            # 16000 cars
    assert auto_delay(16800, LISTING_CHUNK) > 0


def test_a_cached_run_is_not_paced():
    """16000 cars with every number cached is 800 listing requests and nothing
    else -- below the budget, so it must run flat out."""
    assert auto_delay(800, DETAIL_CHUNK) == 0.0


def test_delay_holds_the_safe_rate():
    delay = auto_delay(16800, DETAIL_CHUNK)
    # A chunk of DETAIL_CHUNK requests takes ~2.8s plus the pause.
    rate = DETAIL_CHUNK / (2.8 + delay)
    assert 3.5 <= rate <= 4.5, rate


def test_an_explicit_delay_wins():
    assert paced(0.0, 16800, DETAIL_CHUNK, "detail") == 0.0
    assert paced(7.5, 21, DETAIL_CHUNK, "detail") == 7.5


def test_budget_is_the_boundary():
    assert auto_delay(RATE_BUDGET, DETAIL_CHUNK) == 0.0
    assert auto_delay(RATE_BUDGET + 1, DETAIL_CHUNK) > 0.0


# ── model_query: the search expression ───────────────────────────────────────

# What the site itself puts in the URL for this model, copied from the page.
SITE_EXPRESSION = ("(And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._."
                   "(C.ModelGroup.3시리즈._.Model.3시리즈 (G20_).))))")


def test_full_model_matches_what_the_site_produces():
    assert model_query({"manufacturer": "BMW", "model_group": "3시리즈",
                        "model": "3시리즈 (G20_)"}) == SITE_EXPRESSION


def test_group_and_make_stop_at_the_level_given():
    assert model_query({"manufacturer": "BMW", "model_group": "3시리즈"}) == (
        "(And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._.ModelGroup.3시리즈.)))")
    assert model_query({"manufacturer": "벤츠"}) == (
        "(And.Hidden.N._.(C.CarType.N._.Manufacturer.벤츠.))")


def test_a_gap_ends_the_chain():
    """A model with no model_group cannot be expressed -- the level it hangs
    off is missing -- so it stops at the manufacturer rather than producing
    something malformed."""
    assert model_query({"manufacturer": "BMW", "model": "3시리즈 (G20_)"}) == (
        "(And.Hidden.N._.(C.CarType.N._.Manufacturer.BMW.))")


def test_car_type_defaults_to_imported_and_can_be_set():
    assert ".CarType.N." in model_query({"manufacturer": "BMW"})
    assert ".CarType.Y." in model_query({"car_type": "Y", "manufacturer": "현대"})


def test_both_packages_build_the_same_expression():
    """stock_pipeline keeps its own copy, because the packages are meant to run
    independently. Same models.json, so they have to agree -- and they did not
    when one gained whole-manufacturer support and the other did not: the
    stock_pipeline copy raised KeyError on a `bmw` entry."""
    sys.path.insert(0, os.path.join(ROOT_DIR, "stock_pipeline"))
    from encar_api import model_query as pipeline_query

    for spec in ({"manufacturer": "BMW", "model_group": "3시리즈",
                  "model": "3시리즈 (G20_)"},
                 {"manufacturer": "BMW", "model_group": "3시리즈"},
                 {"manufacturer": "벤츠"},
                 {"car_type": "Y", "manufacturer": "현대"}):
        assert model_query(spec) == pipeline_query(spec), spec


def test_every_models_json_entry_builds():
    """The file is shared, so an entry that only one package can read is a
    broken entry."""
    with open(os.path.join(ROOT_DIR, "models.json"), encoding="utf-8") as fh:
        specs = json.load(fh)
    assert specs, "models.json is empty"
    for spec in specs:
        expression = model_query(spec)
        assert expression.startswith("(And.Hidden.N._."), spec.get("key")
        assert spec.get("label"), f"{spec.get('key')} has no label"


# ── PlateCache ───────────────────────────────────────────────────────────────

def items(*ids):
    return [{"Id": i, "Manufacturer": "BMW", "Model": "3시리즈"} for i in ids]


def test_cache_splits_known_from_unknown():
    cache = PlateCache(enabled=False)
    cache.enabled = True
    cache.entries = {"1": {"car_number": "plate1", "model_info": "BMW"}}
    known, missing = cache.split(items(1, 2, 3))
    assert [k[0]["Id"] for k in known] == [1]
    assert [m["Id"] for m in missing] == [2, 3]
    assert cache.hits == 1


def test_disabled_cache_knows_nothing():
    cache = PlateCache(enabled=False)
    cache.entries = {"1": {"car_number": "plate1"}}
    known, missing = cache.split(items(1, 2))
    assert known == []
    assert len(missing) == 2


def test_a_car_with_no_number_is_not_cached():
    """Otherwise a failed detail call would be remembered as an answer and
    never retried."""
    cache = PlateCache(enabled=True)
    cache.enabled = True
    cache.remember(1, None, "BMW")
    assert cache.entries == {}


def test_cache_survives_a_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.json")
        first = PlateCache(path).load()
        first.remember(42688114, "12가3456", "BMW 3시리즈 (G20) 320i")
        assert first.save() == path

        second = PlateCache(path).load()
        assert second.entries["42688114"]["car_number"] == "12가3456"
        known, missing = second.split(items(42688114, 999))
        assert len(known) == 1 and len(missing) == 1


def test_a_corrupt_cache_is_not_fatal():
    """It only ever means more requests, never a wrong answer."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.json")
        with open(path, "w") as fh:
            fh.write("{not json at all")
        cache = PlateCache(path).load()
        assert cache.entries == {}
        known, missing = cache.split(items(1))
        assert known == [] and len(missing) == 1


def test_entries_without_a_number_are_dropped_on_load():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.json")
        with open(path, "w") as fh:
            json.dump({"1": {"car_number": "ok"}, "2": {"car_number": None},
                       "3": "not a dict"}, fh)
        cache = PlateCache(path).load()
        assert list(cache.entries) == ["1"]


def test_an_entry_past_its_age_is_looked_up_again():
    """The cache cannot tell that an entry is wrong -- nothing in it ever
    changes, by design -- so an age limit is the only thing that corrects one."""
    import time as _time
    cache = PlateCache(enabled=True, max_age_days=90)
    cache.enabled = True
    cache.entries = {
        "1": {"car_number": "fresh", "at": _time.time()},
        "2": {"car_number": "stale", "at": _time.time() - 91 * 86400},
    }
    known, missing = cache.split(items(1, 2))
    assert [k[0]["Id"] for k in known] == [1]
    assert [m["Id"] for m in missing] == [2]
    assert cache.expired == 1


def test_an_expired_entry_is_kept_until_something_replaces_it():
    """Deleting it up front would lose a known car number if the re-fetch then
    fails -- worse than serving one that is merely old."""
    import time as _time
    cache = PlateCache(enabled=True, max_age_days=1)
    cache.enabled = True
    cache.entries = {"1": {"car_number": "stale", "at": _time.time() - 2 * 86400}}
    cache.split(items(1))
    assert cache.entries["1"]["car_number"] == "stale"


def test_no_age_limit_means_entries_never_expire():
    import time as _time
    cache = PlateCache(enabled=True, max_age_days=0)
    cache.enabled = True
    cache.entries = {"1": {"car_number": "ancient", "at": _time.time() - 9999 * 86400}}
    known, _ = cache.split(items(1))
    assert len(known) == 1


def test_entries_from_before_ages_existed_start_the_clock_now():
    """Expiring them instead would re-fetch every car on the first run after an
    upgrade -- 16000 requests to enforce a limit that was not in effect yet."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.json")
        with open(path, "w") as fh:
            json.dump({"1": {"car_number": "old", "model_info": ""}}, fh)

        cache = PlateCache(path, max_age_days=90).load()
        known, missing = cache.split(items(1))
        assert len(known) == 1 and not missing


def test_prune_drops_cars_that_are_gone():
    cache = PlateCache(enabled=True, model_key="bmw")
    cache.enabled = True
    for i in range(5):
        cache.remember(i, f"p{i}", "BMW")
    assert cache.prune_to([0, 1, 2], "bmw") == 2
    assert sorted(cache.entries) == ["0", "1", "2"]


def test_prune_never_touches_another_model():
    """One file serves every model. Pruning after a BMW run must not delete
    the Benz cars it did not ask about -- that would turn a tidy-up into
    thousands of fresh requests on the next Benz run.
    """
    cache = PlateCache(enabled=True)
    cache.enabled = True
    cache.remember(1, "p1", "BMW", model_key="bmw")
    cache.remember(2, "p2", "벤츠", model_key="benz")
    assert cache.prune_to([], "bmw") == 1
    assert list(cache.entries) == ["2"]


def test_prune_leaves_untagged_entries_alone():
    """Entries written before they carried a model. An over-large cache costs
    bytes; deleting a live entry costs a request."""
    cache = PlateCache(enabled=True)
    cache.enabled = True
    cache.entries = {"1": {"car_number": "p1"}}
    assert cache.prune_to([], "bmw") == 0
    assert list(cache.entries) == ["1"]


def test_nothing_is_written_when_nothing_was_added():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.json")
        assert PlateCache(path).load().save() is None
        assert not os.path.exists(path)


# ── --race: running the collector while a cheaper route is tried ─────────────

def quiet(_msg):
    pass


def test_a_background_route_returns_what_it_produced():
    import time as _time

    def slow():
        _time.sleep(0.05)
        return {"rows": rows(20)}

    bg = main.BackgroundRoute("collector", slow, quiet)
    run, reason = bg.result()
    assert reason is None
    assert len(run["rows"]) == 20


def test_a_background_route_reports_its_failure_like_a_direct_call():
    """The loop treats both the same way, so a thread that raised has to come
    back as (None, reason) rather than as an exception on join()."""
    def boom():
        raise SystemExit("Could not read page 1: HTTP 407")

    run, reason = main.BackgroundRoute("collector", boom, quiet).result()
    assert run is None
    assert "407" in reason


def test_a_race_starts_only_the_collector_and_only_if_it_is_still_ahead():
    started = []

    def marker(name):
        return lambda: started.append(name) or {"rows": rows(1)}

    ladder = [("api-direct", None, marker("api-direct")),
              ("page-local", None, marker("page-local")),
              ("collector", None, marker("collector"))]

    # api-direct just failed: the collector is still ahead, so it starts.
    bg = main.start_race(ladder, 0, quiet)
    assert bg is not None and bg.name == "collector"
    bg.result()
    assert started == ["collector"]

    # page-local failed but the collector is already running/behind: nothing
    # new is started, and page-local itself is never started in the background.
    assert main.start_race(ladder, 2, quiet) is None


def test_only_a_blocked_route_is_worth_racing():
    """Racing costs a billed collector run, so it only pays when the collector
    is immune to what went wrong. A misspelt --model fails every route the
    same way -- and did start a collector run before this check existed."""
    for reason in ("SystemExit: Could not read page 1: HTTP 407",
                   "SystemExit: Could not read page 1: HTTP 402",
                   "ReadTimeout: read timed out",
                   "no rows",
                   "only 600/1130 cars, 27 listing page(s) never landed"):
        assert main.worth_racing(reason), reason

    for reason in ("SystemExit: Unknown model 'nope'. models.json has: bmw",
                   "SystemExit: models.json not found at /x/models.json",
                   "RuntimeError: Bright Data is not configured"):
        assert not main.worth_racing(reason), reason


def test_a_race_does_not_start_a_route_that_would_be_skipped():
    """No Bright Data credentials means the collector is skipped in the ladder;
    racing it would start a run that cannot work."""
    ladder = [("api-direct", None, lambda: None),
              ("collector", "no Bright Data credentials in .env",
               lambda: {"rows": rows(1)})]
    assert main.start_race(ladder, 0, quiet) is None


# ── page numbering across routes of different page sizes ─────────────────────

def test_every_page_needs_no_translation():
    assert main.rescale_pages(None, 200, 20) is None


def test_one_api_page_covers_ten_browser_pages():
    assert main.rescale_pages([1], 200, 20) == list(range(1, 11))
    assert main.rescale_pages([2], 200, 20) == list(range(11, 21))


def test_translation_covers_the_same_cars():
    """Cars 200-399 are API page 2, and browser pages 11-20 at 20 a page."""
    api_page, api_limit, browser_limit = 2, 200, 20
    browser_pages = main.rescale_pages([api_page], api_limit, browser_limit)
    first = (browser_pages[0] - 1) * browser_limit
    last = browser_pages[-1] * browser_limit - 1
    assert first == (api_page - 1) * api_limit
    assert last == api_page * api_limit - 1


def test_a_matching_page_size_is_left_alone():
    assert main.rescale_pages([3, 4], 20, 20) == [3, 4]


# ── stock_pipeline: reusing what the database already knows ──────────────────

def pipeline_apply_known():
    sys.path.insert(0, os.path.join(ROOT_DIR, "stock_pipeline"))
    from crawl_listings import apply_known
    return apply_known


def listing_rows(*car_ids):
    return [{"car_id": str(i), "vehicle_no": None, "model_info": "BMW 3시리즈",
             "color_name": None, "mileage": 50000, "price": 4000}
            for i in car_ids]


def test_a_stored_car_number_means_no_detail_request():
    apply_known = pipeline_apply_known()
    rows = listing_rows(1, 2, 3)
    known = {"1": {"vehicle_no": "12가3456", "color_name": "흰색",
                   "model_info": "BMW 3시리즈 (G20) 320i"}}

    remaining = apply_known(rows, known)
    assert [r["car_id"] for r in remaining] == ["2", "3"]
    assert rows[0]["vehicle_no"] == "12가3456"
    assert rows[0]["color_name"] == "흰색"


def test_moving_fields_are_not_taken_from_the_database():
    """Mileage and price change between runs and come from the listing, so a
    stored row must not overwrite what this crawl just read."""
    apply_known = pipeline_apply_known()
    rows = listing_rows(1)
    rows[0]["mileage"], rows[0]["price"] = 61000, 3800
    apply_known(rows, {"1": {"vehicle_no": "12가3456", "mileage": 50000,
                             "price": 4000}})
    assert rows[0]["mileage"] == 61000
    assert rows[0]["price"] == 3800


def test_a_stored_row_without_a_number_is_still_fetched():
    """The point of the lookup is the vehicle number. A row that never got one
    has to be asked about again."""
    apply_known = pipeline_apply_known()
    rows = listing_rows(1)
    remaining = apply_known(rows, {"1": {"vehicle_no": None, "color_name": "흰색"}})
    assert len(remaining) == 1


def test_nothing_known_means_everything_is_fetched():
    apply_known = pipeline_apply_known()
    rows = listing_rows(1, 2)
    assert len(apply_known(rows, {})) == 2


# ── throttle memory ──────────────────────────────────────────────────────────

def test_an_account_refusal_parks_far_longer_than_a_throttle():
    """A throttle clears in minutes. Out of credit for the month, or a zone
    without KYC, does not -- and a route that keeps retrying those burns the
    little that is left."""
    throttled, refused, spent = {}, {}, {}
    main.park(throttled, "api-direct", "SystemExit: HTTP 407", 300.0)
    main.park(refused, "api-brightdata", "SystemExit: HTTP 402", 300.0)
    main.park(spent, "api-scrapingbee",
              'HTTP 401 {"message":"Monthly API calls limit reached: 1000"}', 300.0)

    assert throttled["api-direct"]["cooldown"] == 300.0
    assert refused["api-brightdata"]["cooldown"] == main.ZONE_REFUSED_COOLDOWN
    assert spent["api-scrapingbee"]["cooldown"] == main.ZONE_REFUSED_COOLDOWN


def test_a_spent_quota_is_worth_racing_past():
    """It is an account fact, not a bug in the request -- the collector is
    unaffected by it, so the race is worth starting."""
    assert main.worth_racing('HTTP 401 {"message":"Monthly API calls limit '
                             'reached: 1000"}')


def test_a_cleared_cooldown_lets_the_route_back():
    import time
    state = {"api-direct": {"at": time.time() - 400, "cooldown": 300.0,
                            "why": "throttled"}}
    assert main.parked_for(state, "api-direct", 300.0) is None
    state["api-direct"]["at"] = time.time() - 10
    assert main.parked_for(state, "api-direct", 300.0) is not None


def test_state_written_by_an_older_version_still_reads():
    import time
    assert main.parked_for({"api-direct": time.time() - 10}, "api-direct", 300.0)
    assert main.parked_for({"api-direct": "nonsense"}, "api-direct", 300.0) is None
    assert main.parked_for({}, "api-direct", 300.0) is None


def test_throttle_statuses_are_recognised():
    for status in ("403", "407", "429"):
        assert main.looks_throttled(f"SystemExit: HTTP {status}")
    assert not main.looks_throttled("SystemExit: HTTP 404")
    assert not main.looks_throttled("TimeoutError: read timed out")


def main_() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = []

    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
