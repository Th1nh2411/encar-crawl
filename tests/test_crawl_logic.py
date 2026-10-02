#!/usr/bin/env python3
"""
test_crawl_logic.py -- The decisions the crawl makes, tested without a network.

    python tests/test_crawl_logic.py

Each of these was wrong at some point, or would be expensive to find wrong:

  verdict       a run that lost six listing pages still reported "1020/1020
                car numbers" and called itself ok.
  auto_delay    pacing keyed on a guess slowed a 33s run down to 25 minutes.
  model_query   the expression changes shape at its innermost level, and has
                to match the string the site itself produces.
  guards        a partial crawl must never mark live cars as sold.
  store         merging must never blank a field; a car number once stored is
                never asked for again; one miss is not a sale.

Plain asserts and a main(), so it runs anywhere python does.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from encar import ladder, store  # noqa: E402
from encar.api import DETAIL_CHUNK, LISTING_CHUNK, RATE_BUDGET, auto_delay, paced  # noqa: E402
from encar.cli import parse_pages  # noqa: E402
from encar.guards import evaluate  # noqa: E402
from encar.models import model_query, plate_query  # noqa: E402
from encar.parse import (apply_detail, car_id_from_url, detail_fields,  # noqa: E402
                         detail_fields_from_html, empty_row, listing_row, split_known)


def rows(count: int, with_number: bool = True):
    return [{"car_id": str(i), "vehicle_no": f"plate{i}" if with_number else None}
            for i in range(count)]


def quiet(_msg):
    pass


# ── verdict: is this route's run good enough to keep? ─────────────────────────

def test_verdict_accepts_a_complete_run():
    assert ladder.verdict({"rows": rows(1130), "expectedCars": 1130}, 0.8) is None


def test_verdict_rejects_missing_cars_even_when_every_row_is_filled():
    run = {"rows": rows(1020), "expectedCars": 1130,
           "pagesFailed": [48, 50, 54, 55, 56, 57]}
    why = ladder.verdict(run, 0.8)
    assert why and "1020/1130" in why and "6 listing page(s)" in why, why


def test_verdict_tolerates_one_unlucky_page():
    assert ladder.verdict({"rows": rows(1110), "expectedCars": 1130}, 0.8) is None


def test_verdict_rejects_empty_car_numbers():
    run = {"rows": rows(20, with_number=False) + rows(5), "expectedCars": 25}
    assert "car numbers" in (ladder.verdict(run, 0.8) or "")


def test_verdict_rejects_nothing_at_all():
    assert ladder.verdict({"rows": []}, 0.8) == "no rows"


def test_verdict_without_an_expected_count():
    """The collector cannot report what it did not fetch."""
    assert ladder.verdict({"rows": rows(186), "partial": True}, 0.8) is None


# ── the ladder ───────────────────────────────────────────────────────────────

def step(name, result=None, error=None, skip=None):
    def call():
        if error:
            raise RuntimeError(error)
        return {"route": name, "rows": result or []}
    return (name, skip, call)


def test_the_ladder_stops_at_the_first_route_that_delivers():
    called = []
    ladder_ = [step("api-direct", error="Could not read page 1: HTTP 407"),
               ("page-local", None, lambda: called.append(1) or
                {"route": "page-local", "rows": rows(20)}),
               ("collector", None, lambda: called.append(2) or {"rows": rows(1)})]
    state = {}
    run, attempts = ladder.run_ladder(ladder_, state)
    assert run["route"] == "page-local" and called == [1]
    assert attempts[0]["route"] == "api-direct" and "407" in attempts[0]["result"]
    assert "api-direct" in state, "a throttled route is parked"


def test_a_short_run_is_kept_when_nothing_better_lands():
    short = {"route": "api-direct", "rows": rows(1020), "expectedCars": 1130}
    run, _ = ladder.run_ladder([("api-direct", None, lambda: short),
                                step("page-local", error="blocked")], {})
    assert run is short and "1020/1130" in run["incomplete"]


def test_a_parked_route_is_skipped():
    state = {"api-direct": {"at": time.time(), "cooldown": 300.0, "why": "throttled"}}
    run, attempts = ladder.run_ladder(
        [step("api-direct", rows(5)), step("page-local", rows(5))], state)
    assert run["route"] == "page-local"
    assert attempts[0]["result"].startswith("skipped")


def test_an_account_refusal_parks_far_longer_than_a_throttle():
    throttled, refused, spent = {}, {}, {}
    ladder.park(throttled, "api-direct", "HTTP 407", 300.0)
    ladder.park(refused, "api-brightdata", "HTTP 402", 300.0)
    ladder.park(spent, "api-scrapingbee", 'HTTP 401 "Monthly API calls limit reached"', 300.0)
    assert throttled["api-direct"]["cooldown"] == 300.0
    assert refused["api-brightdata"]["cooldown"] == ladder.ZONE_REFUSED_COOLDOWN
    assert spent["api-scrapingbee"]["cooldown"] == ladder.ZONE_REFUSED_COOLDOWN


def test_a_cleared_cooldown_lets_the_route_back():
    state = {"api-direct": {"at": time.time() - 400, "cooldown": 300.0}}
    assert ladder.parked_for(state, "api-direct", 300.0) is None
    state["api-direct"]["at"] = time.time() - 10
    assert ladder.parked_for(state, "api-direct", 300.0) is not None
    assert ladder.parked_for({"x": "nonsense"}, "x", 300.0) is None


def test_only_a_blocked_route_is_worth_racing():
    for reason in ("HTTP 407", "HTTP 402", "ReadTimeout: read timed out", "no rows",
                   "only 600/1130 cars"):
        assert ladder.worth_racing(reason), reason
    for reason in ("SystemExit: Unknown model 'nope'",
                   "RuntimeError: Bright Data is not configured"):
        assert not ladder.worth_racing(reason), reason


def test_a_race_starts_only_the_collector_and_only_if_it_is_ahead():
    started = []
    steps = [("api-direct", None, lambda: None),
             ("collector", None, lambda: started.append(1) or {"rows": rows(1)})]
    bg = ladder.start_race(steps, 0, quiet)
    assert bg is not None and bg.result()[0] is not None and started == [1]
    assert ladder.start_race(steps, 1, quiet) is None
    assert ladder.start_race([steps[0], ("collector", "no token", steps[1][2])], 0,
                             quiet) is None


# ── pacing ───────────────────────────────────────────────────────────────────

def test_no_delay_below_the_budget():
    assert auto_delay(21, DETAIL_CHUNK) == 0.0
    assert auto_delay(RATE_BUDGET, DETAIL_CHUNK) == 0.0
    assert auto_delay(RATE_BUDGET + 1, DETAIL_CHUNK) > 0.0


def test_delay_holds_the_safe_rate():
    delay = auto_delay(16800, DETAIL_CHUNK)
    assert 3.5 <= DETAIL_CHUNK / (2.8 + delay) <= 4.5
    assert auto_delay(16800, LISTING_CHUNK) > 0


def test_an_explicit_delay_wins():
    assert paced(0.0, 16800, DETAIL_CHUNK, "detail") == 0.0
    assert paced(7.5, 21, DETAIL_CHUNK, "detail") == 7.5


# ── queries ──────────────────────────────────────────────────────────────────

SITE_EXPRESSION = ("(And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._."
                   "(C.ModelGroup.3시리즈._.Model.3시리즈 (G20_).))))")


def test_full_model_matches_what_the_site_produces():
    assert model_query({"manufacturer": "BMW", "model_group": "3시리즈",
                        "model": "3시리즈 (G20_)"}) == SITE_EXPRESSION


def test_the_query_stops_at_the_level_given():
    assert model_query({"manufacturer": "BMW", "model_group": "3시리즈"}) == (
        "(And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._.ModelGroup.3시리즈.)))")
    assert model_query({"manufacturer": "벤츠"}) == (
        "(And.Hidden.N._.(C.CarType.N._.Manufacturer.벤츠.))")
    assert model_query({"manufacturer": "BMW", "model": "x"}) == (
        "(And.Hidden.N._.(C.CarType.N._.Manufacturer.BMW.))")
    assert ".CarType.Y." in model_query({"car_type": "Y", "manufacturer": "현대"})


def test_a_plate_search_is_not_limited_to_imports():
    """The stock is mostly Korean cars; CarType.N would call them all sold."""
    assert "CarType" not in plate_query("110모5877")


def test_every_models_json_entry_builds():
    with open(os.path.join(ROOT_DIR, "models.json"), encoding="utf-8") as fh:
        specs = json.load(fh)
    for spec in specs:
        assert model_query(spec).startswith("(And.Hidden.N._."), spec.get("key")
        assert spec.get("label"), f"{spec.get('key')} has no label"


def test_pages():
    assert parse_pages("all") is None
    assert parse_pages("3") == [3]
    assert parse_pages("2-4") == [2, 3, 4]


# ── parsing: every route lands in the same row shape ─────────────────────────

LISTING = {"Id": 42, "Manufacturer": "BMW", "Model": "3시리즈 (G20)", "Badge": "320i",
           "FormYear": 2021, "Year": 202103.0, "Mileage": 53137.0, "Price": 3290.0,
           "FuelType": "가솔린", "Transmission": "오토", "ServiceCopyCar": "DUPLICATION"}
DETAIL = {"vehicleNo": "300조6742",
          "category": {"manufacturerName": "BMW", "modelName": "3시리즈 (G20)",
                       "gradeName": "320i", "gradeDetailName": "M 스포츠",
                       "modelGroupName": "3시리즈", "yearMonth": "202103"},
          "spec": {"colorName": "청색", "mileage": 53200, "fuelName": "가솔린"}}


def test_a_listing_row_carries_the_listing_fields():
    row = listing_row(LISTING, page=2)
    assert row["car_id"] == "42" and row["page"] == 2
    assert row["price"] == 3290 and row["mileage"] == 53137 and row["form_year"] == "2021"
    assert row["is_duplicate"] == 1 and row["vehicle_no"] is None


def test_the_detail_adds_the_number_and_the_sharper_model():
    row = listing_row(LISTING)
    apply_detail(row, detail_fields(DETAIL))
    assert row["vehicle_no"] == "300조6742"
    assert row["model_info"] == "BMW 3시리즈 (G20) 320i M 스포츠"
    assert row["color_name"] == "청색" and row["mileage"] == 53200
    assert row["price"] == 3290, "the detail must not touch listing-only fields"


def test_the_detail_page_html_gives_the_same_fields_as_the_api():
    html = ('<script>{"vehicleNo":"300조6742","category":{"manufacturerName":"BMW",'
            '"modelName":"3시리즈 (G20)","gradeName":"320i","gradeDetailName":"M 스포츠",'
            '"formYear":2021},"spec":{"colorName":"청색"}}</script>'
            '<script>{"vehicleNo":"999가9999"}</script>')
    fields = detail_fields_from_html(html)
    assert fields["vehicle_no"] == "300조6742", "the first record is the car itself"
    assert fields["form_year"] == "2021" and fields["color_name"] == "청색"
    a, b = listing_row(LISTING), listing_row(LISTING)
    apply_detail(a, fields)
    apply_detail(b, detail_fields(DETAIL))
    assert a["model_info"] == b["model_info"] and a["vehicle_no"] == b["vehicle_no"]


def test_car_id_from_a_detail_url():
    assert car_id_from_url("https://fem.encar.com/cars/detail/42688114") == "42688114"
    assert car_id_from_url("http://x/dc_cardetailview.do?carid=7") == "7"
    assert car_id_from_url("nope") is None


def test_a_stored_number_means_no_detail_request():
    fresh = [listing_row(dict(LISTING, Id=i)) for i in (1, 2)]
    reused, missing = split_known(fresh, {"1": {"vehicle_no": "12가3456",
                                                "color_name": "흰색", "price": 1}})
    assert [r["car_id"] for r in reused] == ["1"] and [r["car_id"] for r in missing] == ["2"]
    assert fresh[0]["vehicle_no"] == "12가3456" and fresh[0]["color_name"] == "흰색"
    assert fresh[0]["price"] == 3290, "moving fields come from today's listing"
    _, missing = split_known([listing_row(LISTING)], {"42": {"vehicle_no": None}})
    assert len(missing) == 1, "a stored row without a number is asked about again"


# ── guards: when may missing cars be marked? ─────────────────────────────────

def full_run(n=1130, **extra):
    run = {"rows": rows(n), "everyPage": True, "listingTotal": 1130,
           "pagesRequested": 6, "pagesFetched": 6, "expectedCars": 1130}
    run.update(extra)
    return run


def test_a_complete_crawl_may_mark():
    assert evaluate(full_run(), 1100)[:2] == ("OK", True)


def test_a_page_subset_never_marks():
    assert evaluate(full_run(everyPage=False), None)[1] is False


def test_a_lost_page_never_marks():
    assert evaluate(full_run(pagesFetched=5), None)[1] is False


def test_short_pages_never_mark():
    """Every page answered, but fewer cars than Encar said were there."""
    assert evaluate(full_run(n=1000), None)[1] is False


def test_a_collapse_against_the_last_good_run_never_marks():
    run = full_run(n=500, listingTotal=500, expectedCars=500)
    assert evaluate(run, 1130)[1] is False


# ── store: one database, merged ──────────────────────────────────────────────

def db():
    return store.connect(":memory:")


def row(car_id, **fields):
    r = empty_row(car_id)
    r.update(fields)
    return r


def test_a_sparse_route_never_blanks_a_rich_row():
    conn = db()
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="12가3456", price=3000,
                                          color_name="흰색", is_duplicate=1)], 1, ["1"])
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="12가3456",
                                          model_info="BMW 320i")], 2)
    got = dict(conn.execute("SELECT * FROM vehicles").fetchone())
    assert got["price"] == 3000 and got["color_name"] == "흰색" and got["is_duplicate"] == 1
    assert got["model_info"] == "BMW 320i" and got["last_batch_id"] == 2


def test_known_details_are_the_cache():
    conn = db()
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="12가3456"), row(2)], 1, ["1"])
    known = store.known_details(conn)
    assert list(known) == ["1"] and known["1"]["vehicle_no"] == "12가3456"


def test_an_old_lookup_is_asked_again():
    conn = db()
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="12가3456")], 1, ["1"])
    conn.execute("UPDATE vehicles SET detail_at = '2020-01-01 00:00:00'")
    assert store.known_details(conn, max_age_days=90) == {}
    assert list(store.known_details(conn, max_age_days=0)) == ["1"]


def test_one_miss_is_not_a_sale_and_a_return_resets_it():
    conn = db()
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="a"), row(2, vehicle_no="b")], 1)
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="a")], 2)
    assert store.mark_missing(conn, "m", 2, sold_after=2) == (1, 0)
    status = conn.execute("SELECT status FROM car_status WHERE plate = 'b'").fetchone()[0]
    assert status == "CHECKING"

    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="a")], 3)
    assert store.mark_missing(conn, "m", 3, sold_after=2) == (1, 1)
    assert conn.execute("SELECT status FROM car_status WHERE plate = 'b'").fetchone()[0] == "SOLD"

    store.upsert_vehicles(conn, "m", [row(2, vehicle_no="b")], 4)
    got = conn.execute("SELECT status, missing_streak FROM vehicles WHERE car_id = '2'").fetchone()
    assert tuple(got) == ("ACTIVE", 0), "a relisted car is live again"


def test_another_models_listings_are_never_marked():
    conn = db()
    store.upsert_vehicles(conn, "bmw", [row(1, vehicle_no="a")], 1)
    store.upsert_vehicles(conn, "benz", [row(2, vehicle_no="b")], 2)
    assert store.mark_missing(conn, "benz", 2, sold_after=1) == (0, 0)


def test_a_car_is_sold_only_when_every_ad_is_gone():
    conn = db()
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="p"), row(2, vehicle_no="p")], 1)
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="p")], 2)
    store.mark_missing(conn, "m", 2, sold_after=1)
    got = dict(conn.execute("SELECT * FROM car_status WHERE plate = 'p'").fetchone())
    assert got["status"] == "ON_SALE" and got["listings"] == 2 and got["active_listings"] == 1


def test_stock_and_encar_meet_in_one_view():
    conn = db()
    store.upsert_vehicles(conn, "m", [row(1, vehicle_no="12가3456")], 1)
    stock = [{"product_id": 7, "car_number": "12가3456", "plate_valid": 1, "imported_at": "x"},
             {"product_id": 8, "car_number": "34나5678", "plate_valid": 1, "imported_at": "x"},
             {"product_id": 9, "car_number": "test 01", "plate_valid": 0, "imported_at": "x"}]
    store.replace_stock(conn, [], stock)
    got = {r["plate"]: (r["status"], r["in_stock"]) for r in
           conn.execute("SELECT plate, status, in_stock FROM car_status")}
    assert got["12가3456"] == ("ON_SALE", 1)
    assert got["34나5678"] == ("NOT_FOUND", 1), "never crawled is not sold"
    assert store.status_counts(conn)["NOT_FOUND"] == 1, "placeholder plates are hidden"


def test_the_old_plate_cache_is_taken_over_once():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "plate_cache.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"1": {"car_number": "12가3456", "model_info": "BMW",
                             "model": "m", "at": time.time()},
                       "2": {"car_number": None}}, fh)
        conn = db()
        assert store.import_plate_cache(conn, path) == 1
        assert store.import_plate_cache(conn, path) == 0, "existing rows are left alone"
        assert list(store.known_details(conn)) == ["1"]


def test_route_state_round_trips():
    conn = db()
    state = {}
    ladder.park(state, "api-direct", "HTTP 407", 300.0)
    store.save_route_state(conn, state)
    assert store.load_route_state(conn)["api-direct"]["cooldown"] == 300.0


def main_() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    ladder_log = ladder.log
    ladder.log = quiet  # the ladder narrates; tests do not need to hear it
    failed = []
    try:
        for name, fn in tests:
            try:
                fn()
            except Exception as exc:
                failed.append(name)
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    finally:
        ladder.log = ladder_log
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
