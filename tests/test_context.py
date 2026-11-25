"""The context columns and the context tab. No network: every feed is a fixture.

The EIA and CFTC payloads below are small shapes built from the API docs, NOT
captured from the live services (both are blocked on the development machine).
`python -m scripts.context_check` on a GitHub runner is the real check.
"""

import statistics
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from src import context, main, news, scorer, sheets
from src.context import FeedResult
from src.sheets import CONTEXT, SCHEMAS, SIGNALS
from tests.fakes import FakeSheets
from tests.test_main import (  # noqa: F401 - wired is a fixture
    _FrozenDatetime, fake_headline, wire_news, wire_price, wire_score, wired,
)

UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


# ---------------------------------------------------------------- schema ----

ORIGINAL_SIGNALS = [
    "run_utc", "run_chicago", "rules_version", "market_open", "brent_price",
    "price_time_utc", "new_headline_count", "score", "confidence", "direction",
    "new_information", "key_headlines", "reasoning", "action", "position_after",
    "notes", "price_1h", "price_4h", "price_24h", "price_72h",
    "move_1h_pct", "move_4h_pct", "move_24h_pct", "move_72h_pct", "score_source",
]


def test_signals_columns_are_appended_in_the_agreed_order():
    columns = SCHEMAS[SIGNALS]
    assert columns[:25] == ORIGINAL_SIGNALS
    assert columns[25:] == ["context_version", "vol20_pct", "ma200", "event_flags"]


def test_the_context_tab_exists_is_keyed_by_run_utc_and_is_backed_up():
    assert CONTEXT in SCHEMAS
    assert SCHEMAS[CONTEXT][0] == "run_utc"
    assert len(set(SCHEMAS[CONTEXT])) == len(SCHEMAS[CONTEXT])
    # scripts/backup.py exports every tab in SCHEMAS.
    assert "SCHEMAS" in open("scripts/backup.py").read()


# ----------------------------------------------------------------- maths ----

def test_vol20_is_the_sample_stdev_of_the_last_20_percent_returns():
    prices = [100.0]
    for i in range(30):
        prices.append(prices[-1] * (1.01 if i % 2 == 0 else 0.98))
    returns = [(b / a - 1) * 100 for a, b in zip(prices[-21:], prices[-20:])]
    expected = statistics.stdev(returns)  # statistics.stdev divides by n minus 1
    assert context.vol_pct(prices, 20) == pytest.approx(expected)
    assert statistics.pstdev(returns) != pytest.approx(expected)  # so the choice matters


def test_vol_needs_21_prices_for_20_returns():
    assert context.vol_pct([100.0 + i for i in range(20)], 20) is None
    assert context.vol_pct([100.0 + i for i in range(21)], 20) is not None


def test_constant_returns_have_zero_vol():
    prices = [100 * 1.01 ** i for i in range(25)]
    assert context.vol_pct(prices, 20) == pytest.approx(0.0, abs=1e-9)


def test_ma200_is_the_mean_of_the_last_200_points_with_live_price_last():
    series = [("d%d" % i, float(i)) for i in range(1, 200)]  # 199 points: 1..199
    got = context.daily_metrics(series + [("x", 200.0)], 200.0)
    assert got["ma200"] == 100.5  # mean of 1..200
    # 199 points is too few.
    assert context.daily_metrics(series, 199.0)["ma200"] == ""


def test_ma200_is_blank_without_a_live_price():
    series = [("d%d" % i, float(i)) for i in range(1, 251)]
    got = context.daily_metrics(series, None)
    assert got["ma200"] == ""
    assert got["dist_ma200_pct"] == ""
    assert got["ma50"] != ""


def test_distance_momentum_and_range_position():
    series = [("d%03d" % i, 100.0 + i) for i in range(300)]  # 100..399
    got = context.daily_metrics(series, 399.0)
    last200 = [100.0 + i for i in range(100, 300)]
    assert got["ma200"] == round(sum(last200) / 200, 2)
    assert got["dist_ma200_pct"] == round((399.0 / (sum(last200) / 200) - 1) * 100, 2)
    assert got["mom_21d_pct"] == round((399.0 / 378.0 - 1) * 100, 2)
    assert got["mom_252d_pct"] == round((399.0 / 147.0 - 1) * 100, 2)
    assert got["range20_pos"] == 1.0


def test_range_position_is_blank_for_a_flat_or_short_window():
    assert context.range_position([5.0] * 20, 20) is None
    assert context.range_position([5.0] * 19, 20) is None
    assert context.range_position(list(range(10, 30)), 20) == 1.0


# ------------------------------------------------------------------ FRED ----

FRED_TEXT = """observation_date,DCOILBRENTEU
2026-11-10,90.50
2026-11-11,
2026-11-12,.
2026-11-13,95.17
not a date,1
2026-11-16,92.90
"""


def test_fred_csv_parsing_drops_blank_and_dot_values():
    assert context.parse_fred_csv(FRED_TEXT) == [
        ("2026-11-10", 90.5), ("2026-11-13", 95.17), ("2026-11-16", 92.9)]


def test_fred_csv_parsing_survives_junk():
    assert context.parse_fred_csv("") == []
    assert context.parse_fred_csv("<html>blocked</html>") == []
    assert context.parse_fred_csv(None) == []


def test_the_committed_fallback_file_parses():
    points = context.load_daily_file()
    assert len(points) > 1000
    assert points[0][0] < points[-1][0]


# ------------------------------------------------- log extension of series ----

def test_log_extension_adds_the_last_price_of_each_later_weekday_then_live():
    daily = [("2026-11-12", 90.0), ("2026-11-13", 91.0)]  # a Friday is the last FRED date
    logged = [
        (utc(2026, 11, 13, 9), 91.5),   # same date as FRED: ignored
        (utc(2026, 11, 14, 9), 91.7),   # Saturday: skipped
        (utc(2026, 11, 16, 8), 92.0),   # Monday, two rows: the later one wins
        (utc(2026, 11, 16, 15), 93.0),
        (utc(2026, 11, 17, 9), 94.0),   # Tuesday
    ]
    series, added = context.extend_daily_series(daily, logged, 95.5, utc(2026, 11, 18, 12))
    assert series == daily + [
        ("2026-11-16", 93.0), ("2026-11-17", 94.0), ("2026-11-18", 95.5)]
    assert added == 3


def test_live_price_replaces_a_logged_price_for_the_same_date():
    series, _ = context.extend_daily_series(
        [("2026-11-13", 91.0)], [(utc(2026, 11, 16, 8), 92.0)], 93.3, utc(2026, 11, 16, 15))
    assert series[-1] == ("2026-11-16", 93.3)
    assert len(series) == 2


def test_a_weekend_live_price_stands_in_for_friday_not_a_new_day():
    series, _ = context.extend_daily_series(
        [("2026-11-12", 90.0)], [], 91.1, utc(2026, 11, 14, 12))  # Saturday
    assert series[-1] == ("2026-11-13", 91.1)


def test_no_live_price_means_the_series_ends_at_the_last_known_date():
    series, added = context.extend_daily_series(
        [("2026-11-13", 91.0)], [(utc(2026, 11, 16, 8), 92.0)], None, utc(2026, 11, 17, 8))
    assert series[-1] == ("2026-11-16", 92.0)
    assert added == 1


def test_an_empty_daily_history_still_returns_the_log_points():
    series, _ = context.extend_daily_series([], [(utc(2026, 11, 16, 8), 92.0)], 93.0, utc(2026, 11, 17, 8))
    assert [d for d, _ in series] == ["2026-11-16", "2026-11-17"]


# ----------------------------------------------------------------- events ----

def test_eia_window_follows_new_york_daylight_saving():
    # The US clocks went back on 2026-11-01. Wednesday 10:30 New York is
    # 14:30 UTC before that (EDT) and 15:30 UTC after it (EST).
    edt = utc(2026, 10, 28, 14, 30)
    est = utc(2026, 11, 4, 15, 30)
    assert edt in context.eia_report_times(edt) and est in context.eia_report_times(est)
    # Window is 4 hours before to 2 hours after.
    assert context.in_eia_window(utc(2026, 10, 28, 10, 30))        # start, EDT
    assert not context.in_eia_window(utc(2026, 10, 28, 10, 29))
    assert context.in_eia_window(utc(2026, 10, 28, 16, 30))        # end, EDT
    assert not context.in_eia_window(utc(2026, 10, 28, 16, 31))
    assert context.in_eia_window(utc(2026, 11, 4, 11, 30))         # start, EST
    assert not context.in_eia_window(utc(2026, 11, 4, 11, 29))
    assert context.in_eia_window(utc(2026, 11, 4, 17, 30))         # end, EST
    assert not context.in_eia_window(utc(2026, 11, 4, 17, 31))
    # The same UTC clock time lands on opposite sides of the change.
    assert not context.in_eia_window(utc(2026, 10, 28, 16, 45))
    assert context.in_eia_window(utc(2026, 11, 4, 16, 45))
    assert context.in_eia_window(est)


def test_eia_window_follows_the_spring_change_too():
    # Clocks went forward on 2026-03-08: Wednesday 10:30 is 15:30 UTC (EST)
    # on the 4th and 14:30 UTC (EDT) on the 11th.
    assert context.in_eia_window(utc(2026, 3, 4, 11, 30))
    assert not context.in_eia_window(utc(2026, 3, 4, 11, 29))
    assert context.in_eia_window(utc(2026, 3, 11, 10, 30))
    assert not context.in_eia_window(utc(2026, 3, 11, 10, 29))


def test_eia_window_is_wednesday_only():
    assert not context.in_eia_window(utc(2026, 11, 3, 15, 30))  # Tuesday
    assert not context.in_eia_window(utc(2026, 11, 5, 15, 30))  # Thursday


def test_a_wednesday_window_does_not_leak_over_midnight_in_utc():
    # Wednesday 10:30 New York window, seen from a UTC date that is Thursday
    # never happens (it ends 12:30 New York), but a Tuesday night UTC run before
    # the 4 hour lead in must not flag.
    assert not context.in_eia_window(utc(2026, 11, 4, 3, 0))


def test_hours_to_next_eia():
    assert context.hours_to_next_eia(utc(2026, 11, 4, 14, 30)) == 1.0
    assert context.hours_to_next_eia(utc(2026, 11, 4, 15, 30)) == 0.0
    # Just after the report the next one is a week away.
    assert context.hours_to_next_eia(utc(2026, 11, 4, 15, 31)) == pytest.approx(167.98, abs=0.05)


MEETINGS = [date(2026, 11, 29)]


def test_opec_day_is_the_meeting_date_and_the_day_before():
    assert context.is_opec_day(utc(2026, 11, 29, 12), MEETINGS)
    assert context.is_opec_day(utc(2026, 11, 28, 0, 0), MEETINGS)
    assert context.is_opec_day(utc(2026, 11, 28, 23, 59), MEETINGS)
    assert not context.is_opec_day(utc(2026, 11, 27, 23, 59), MEETINGS)
    assert not context.is_opec_day(utc(2026, 11, 30, 0, 0), MEETINGS)


def test_opec_day_uses_the_utc_date_not_the_local_one():
    new_york_evening = datetime(2026, 11, 27, 20, 0, tzinfo=context.NEW_YORK)  # 01:00 UTC on the 28th
    assert context.is_opec_day(new_york_evening, MEETINGS)


def test_event_flags_join_with_semicolons_and_are_empty_when_none():
    both = [date(2026, 11, 4)]  # a Wednesday
    assert context.event_flags(utc(2026, 11, 4, 15, 0), both) == "eia_window;opec_day"
    assert context.event_flags(utc(2026, 11, 4, 15, 0), []) == "eia_window"
    assert context.event_flags(utc(2026, 11, 2, 15, 0), []) == ""
    assert context.event_flags(utc(2026, 11, 3, 15, 0), both) == "opec_day"


def test_days_to_next_opec():
    assert context.days_to_next_opec(utc(2026, 11, 25, 8), MEETINGS) == 4
    assert context.days_to_next_opec(utc(2026, 11, 29, 8), MEETINGS) == 0
    assert context.days_to_next_opec(utc(2026, 11, 30, 8), MEETINGS) == ""
    assert context.days_to_next_opec(utc(2026, 11, 30, 8), []) == ""


def test_the_committed_meetings_file_has_the_one_entry():
    assert context.load_opec_dates() == [date(2026, 11, 29)]


def test_a_missing_or_empty_meetings_file_is_fail_soft(tmp_path):
    assert context.load_opec_dates(tmp_path / "nope.csv") == []
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    assert context.load_opec_dates(empty) == []
    junk = tmp_path / "junk.csv"
    junk.write_text("date,note\nsoon,maybe\n2026-13-45,bad\n2027-06-01,\"ok, quoted\"\n")
    assert context.load_opec_dates(junk) == [date(2027, 6, 1)]


# ------------------------------------------------------------ price path ----

def _series(*pairs):
    return [(utc(2026, 11, 16, h, m), p) for (h, m, p) in pairs]


def test_prior_moves_use_the_nearest_earlier_row_within_tolerance():
    now = utc(2026, 11, 16, 12, 0)
    logged = [
        (now - timedelta(hours=72, minutes=30), 80.0),   # within 8h of the 72h target
        (now - timedelta(hours=25, minutes=40), 85.0),   # 24h: before the target, within 4h
        (now - timedelta(hours=24, minutes=40), 86.0),   # nearer: this one wins
        (now - timedelta(hours=23, minutes=50), 87.0),   # after the target: not allowed
        (now - timedelta(hours=4, minutes=20), 88.0),    # 4h
        (now - timedelta(minutes=30), 89.0),             # after the 1h target: not allowed
    ]
    got = context.price_path(logged, now, 90.0)
    assert got["prior_move_72h_pct"] == round((90 / 80 - 1) * 100, 2)
    assert got["prior_move_24h_pct"] == round((90 / 86 - 1) * 100, 2)
    assert got["prior_move_4h_pct"] == round((90 / 88 - 1) * 100, 2)
    assert got["prior_move_1h_pct"] == ""


def test_prior_moves_are_blank_when_the_earlier_row_is_too_old_or_price_missing():
    now = utc(2026, 11, 16, 12, 0)
    too_old = [(now - timedelta(hours=1, minutes=50), 88.0)]  # 1h tolerance is 45 minutes
    assert context.price_path(too_old, now, 90.0)["prior_move_1h_pct"] == ""
    ok = [(now - timedelta(hours=1, minutes=20), 88.0)]
    assert context.price_path(ok, now, None)["prior_move_1h_pct"] == ""


def test_run_history_gap_previous_score_and_run_count():
    now = utc(2026, 11, 16, 12, 0)
    rows = [
        {"run_utc": (now - timedelta(hours=30)).isoformat(), "score": 1},
        {"run_utc": (now - timedelta(hours=5)).isoformat(), "score": 3},
        {"run_utc": (now - timedelta(hours=2, minutes=30)).isoformat(), "score": -4},
        {"run_utc": "garbage", "score": 9},
    ]
    got = context.run_history(rows[:3], now)
    assert got == {"hours_since_prev_run": 2.5, "prev_score": -4, "runs_last_24h": 2}
    assert context.run_history([], now) == {
        "hours_since_prev_run": "", "prev_score": "", "runs_last_24h": 0}


# --------------------------------------------------- weekly fundamentals ----

# Built from the documented shapes, not captured from the live APIs.
EIA_PAYLOAD = {"response": {"total": "3", "data": [
    {"period": "2026-11-13", "series": "WCESTUS1", "value": "421000", "units": "MBBL"},
    {"period": "2026-11-06", "series": "WCESTUS1", "value": "423500", "units": "MBBL"},
    {"period": "2026-10-30", "series": "WCESTUS1", "value": "419000", "units": "MBBL"},
]}}

CFTC_ROWS = [
    {"report_date_as_yyyy_mm_dd": "2026-11-10T00:00:00.000",
     "m_money_positions_long_all": "150000", "m_money_positions_short_all": "40000",
     "open_interest_all": "1700000"},
    {"report_date_as_yyyy_mm_dd": "2026-11-17T00:00:00.000",
     "m_money_positions_long_all": "155000", "m_money_positions_short_all": "38000",
     "open_interest_all": "1710000"},
]


def test_eia_parsing_gives_level_change_and_period():
    assert context.parse_eia_stocks(EIA_PAYLOAD) == {
        "level": 421000.0, "change": -2500.0, "period": "2026-11-13"}


def test_eia_parsing_is_order_independent_and_tolerates_odd_rows():
    shuffled = {"response": {"data": list(reversed(EIA_PAYLOAD["response"]["data"])) + [
        {"period": "2026-11-20", "value": None}, "junk", {"value": "5"}]}}
    assert context.parse_eia_stocks(shuffled)["period"] == "2026-11-13"


def test_eia_single_point_has_a_blank_change_and_nothing_raises_a_value_error_once():
    one = {"response": {"data": [{"period": "2026-11-13", "value": "421000"}]}}
    assert context.parse_eia_stocks(one)["change"] == ""
    for bad in ({}, {"response": {}}, {"response": {"data": []}}, None):
        with pytest.raises(ValueError):
            context.parse_eia_stocks(bad)


def test_cftc_parsing_gives_net_change_open_interest_and_date():
    assert context.parse_cftc(CFTC_ROWS) == {
        "net": 117000.0, "change": 117000.0 - 110000.0,
        "open_interest": 1710000.0, "report_date": "2026-11-17"}


def test_cftc_single_row_falls_back_to_the_change_fields():
    row = dict(CFTC_ROWS[1], change_in_m_money_long_all="5000", change_in_m_money_short_all="-2000")
    got = context.parse_cftc([row])
    assert got["change"] == 7000.0
    assert context.parse_cftc([CFTC_ROWS[1]])["change"] == ""


def test_cftc_parsing_rejects_garbage():
    for bad in (None, {}, [], [{"foo": 1}], "text"):
        with pytest.raises(ValueError):
            context.parse_cftc(bad)


# ------------------------------------------------------ fetchers fail soft ----

def test_error_reasons_never_carry_the_url_or_an_api_key():
    leaky = requests.ConnectionError(
        "HTTPSConnectionPool(host='api.eia.gov'): Max retries exceeded with url: "
        "/v2/x?api_key=SECRET123")
    assert context.reason_for(leaky) == "connection"
    assert "SECRET" not in context.reason_for(leaky)
    assert context.reason_for(requests.Timeout("slow")) == "timeout"
    assert context.reason_for(requests.ConnectTimeout("slow")) == "timeout"
    response = SimpleNamespace(status_code=503)
    assert context.reason_for(requests.HTTPError("503 for url ?api_key=SECRET", response=response)) == "http503"
    assert context.reason_for(ValueError("bad")) == "parse"
    assert context.reason_for(KeyError("x")) == "KeyError"


def test_a_failing_fetch_becomes_a_failed_result_not_an_exception(monkeypatch):
    def boom(url, params=None):
        raise requests.ConnectionError("url with api_key=SECRET")
    monkeypatch.setattr(context, "_get", boom)
    result = context._soft(context.eia_stocks)
    assert result == FeedResult(False, None, "connection")


def test_the_eia_key_is_sent_but_defaults_to_demo_key(monkeypatch):
    seen = []

    class Resp:
        def json(self):
            return EIA_PAYLOAD

    monkeypatch.setattr(context, "_get", lambda url, params=None: seen.append(params) or Resp())
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    context.eia_stocks()
    monkeypatch.setenv("EIA_API_KEY", "  mykey ")
    context.eia_stocks()
    assert seen[0]["api_key"] == "DEMO_KEY"
    assert seen[1]["api_key"] == "mykey"
    assert seen[0]["facets[series][]"] == "WCESTUS1"


def test_cftc_request_asks_for_the_wti_contract_newest_first(monkeypatch):
    seen = []

    class Resp:
        def json(self):
            return CFTC_ROWS

    monkeypatch.setattr(context, "_get", lambda url, params=None: seen.append((url, params)) or Resp())
    assert context.cftc_wti()["net"] == 117000.0
    url, params = seen[0]
    assert "publicreporting.cftc.gov" in url
    assert params["cftc_contract_market_code"] == "067651"
    assert "DESC" in params["$order"]


def test_fetch_all_reports_a_hung_feed_as_a_budget_miss(monkeypatch):
    import threading
    release = threading.Event()
    jobs = {"slow": lambda: release.wait(5), "fast": lambda: 1}
    monkeypatch.setattr(context, "feed_jobs", lambda now: jobs)
    got = context.fetch_all.real(utc(2026, 11, 16, 12), budget_seconds=0.2)
    release.set()
    assert got["fast"] == FeedResult(True, 1, "")
    assert got["slow"] == FeedResult(False, None, "budget")


# -------------------------------------------------------------- assemble ----

def good_feeds():
    brent = [("2026-11-%02d" % d, 90.0 + d * 0.1) for d in range(2, 14)]
    wti = [("2026-11-12", 86.0), ("2026-11-13", 87.0), ("2026-11-16", 88.0)]
    return {
        "fred_brent": FeedResult(True, brent),
        "fred_wti": FeedResult(True, wti),
        "fred_ovx": FeedResult(True, [("2026-11-13", 40.0), ("2026-11-16", 41.5)]),
        "fred_usd": FeedResult(True, [("2026-11-13", 120.0)]),
        "eia_stocks": FeedResult(True, context.parse_eia_stocks(EIA_PAYLOAD)),
        "cftc_cot": FeedResult(True, context.parse_cftc(CFTC_ROWS)),
    }


def test_assemble_with_every_feed_good():
    now = utc(2026, 11, 17, 12)
    got = context.assemble(now, 95.0, [], good_feeds(), [], MEETINGS)
    assert got["daily_source"] == "fred"
    assert got["daily_last_date"] == "2026-11-13"
    assert got["daily_log_days_added"] == 1  # only today's live point
    assert got["wti_price"] == 88.0 and got["wti_asof"] == "2026-11-16"
    # Spread on the newest date both series share: 2026-11-13.
    assert got["brent_wti_spread_asof"] == "2026-11-13"
    assert got["brent_wti_spread"] == round(91.3 - 87.0, 2)
    assert got["ovx"] == 41.5 and got["ovx_asof"] == "2026-11-16"
    assert got["usd_broad"] == 120.0
    assert got["eia_crude_stocks_kbbl"] == 421000.0
    assert got["cftc_mm_net"] == 117000.0
    assert got["context_errors"] == ""
    assert got["days_to_next_opec"] == 12


def test_assemble_falls_back_to_the_file_and_records_each_failure():
    feeds = {name: FeedResult(False, None, "timeout") for name in good_feeds()}
    feeds["cftc_cot"] = FeedResult(False, None, "connection")
    file_points = [("2026-11-12", 90.0), ("2026-11-13", 91.0)]
    got = context.assemble(utc(2026, 11, 17, 12), 95.0, [], feeds, file_points, [])
    assert got["daily_source"] == "file"
    assert got["daily_last_date"] == "2026-11-13"
    assert "wti_price" not in got
    assert got["context_errors"].split(";") == [
        "fred_brent:timeout", "fred_wti:timeout", "fred_ovx:timeout", "fred_usd:timeout",
        "eia_stocks:timeout", "cftc_cot:connection"]
    assert got["hours_to_next_eia"] != ""  # events need no network


def test_assemble_with_nothing_at_all_still_returns_events_and_errors():
    feeds = {}
    got = context.assemble(utc(2026, 11, 17, 12), None, [], feeds, [], MEETINGS)
    assert got["daily_source"] == ""
    assert "daily_file:missing" in got["context_errors"]
    assert got["vol20_pct"] == "" and got["ma200"] == ""
    assert got["event_flags"] == ""


def test_assemble_extends_the_series_from_the_signals_rows():
    now = utc(2026, 11, 17, 12)
    rows = [{"run_utc": "2026-11-16T09:00:00+00:00", "price_time_utc": "2026-11-16T08:58:00+00:00",
             "brent_price": "93.5"}]
    got = context.assemble(now, 95.0, rows, good_feeds(), [], [])
    assert got["daily_log_days_added"] == 2  # the logged Monday and today's live price
    assert got["daily_points"] == 12 + 2


def test_every_gathered_and_meta_key_is_a_context_column():
    now = utc(2026, 11, 17, 12)
    gathered = context.assemble(now, 95.0, [], good_feeds(), [], MEETINGS)
    usage = scorer.Usage(input_tokens=10, output_tokens=5, attempts=1, latency_ms=900, stop_reason="end_turn")
    quote = SimpleNamespace(ok=True, price_time_utc=(now - timedelta(minutes=3)).isoformat())
    meta = context.run_meta_columns(now, usage, quote, 120, 5000, environ={})
    news_cols = context.news_columns({"fetched": 3, "errors": ["google_news:ReadTimeout"]}, [], now, 90)
    schema = set(SCHEMAS[CONTEXT])
    for part in (gathered, meta, news_cols):
        assert set(part) <= schema, set(part) - schema
    row = context.build_row(now, gathered, news_cols, meta)
    assert row["run_utc"] == now.isoformat()
    assert row["context_version"] == "c1"


def test_build_row_without_gathered_data_keeps_events_and_the_error():
    now = utc(2026, 11, 4, 15)
    row = context.build_row(now, None, {}, {}, extra_errors=["gather_failed:RuntimeError"])
    assert row["context_errors"] == "gather_failed:RuntimeError"
    assert row["event_flags"] == "eia_window"
    assert row["context_version"] == "c1"


def test_run_meta_has_no_secret_and_reads_the_github_env():
    env = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_EVENT_NAME": "schedule",
           "GITHUB_SHA": "abcdef1234567890", "ANTHROPIC_API_KEY": "sk-secret", "EIA_API_KEY": "k"}
    meta = context.run_meta_columns(utc(2026, 11, 17, 12), None, None, None, None, environ=env)
    assert meta["github_run_id"] == "123"
    assert meta["github_run_attempt"] == "2"
    assert meta["trigger"] == "schedule"
    assert meta["git_sha"] == "abcdef1"
    assert "sk-secret" not in str(meta) and "'k'" not in str(meta)
    assert len(meta["prompt_hash"]) == 12


def test_news_columns_count_sources_and_ages():
    now = utc(2026, 11, 17, 12)
    fresh = [
        news.Headline("h1", (now - timedelta(minutes=5)).isoformat(), "Reuters", "a", "u"),
        news.Headline("h2", (now - timedelta(minutes=65)).isoformat(), "Reuters", "b", "u"),
        news.Headline("h3", (now - timedelta(minutes=35)).isoformat(), "Bloomberg", "c", "u"),
    ]
    cols = context.news_columns(
        {"fetched": 10, "outside_window": 2, "duplicates_dropped": 4, "over_cap_dropped": 0,
         "errors": ["google_news:ConnectionError"]}, fresh, now, 120)
    assert cols["news_new_sent"] == 3
    assert cols["news_distinct_sources"] == 2
    assert cols["news_newest_age_min"] == 5.0
    assert cols["news_oldest_age_min"] == 65.0
    assert cols["news_fetched"] == 10 and cols["news_duplicates_dropped"] == 4
    assert cols["news_feed_errors"] == "google_news:ConnectionError"
    empty = context.news_columns(None, [], now, None)
    assert empty["news_new_sent"] == 0 and empty["news_newest_age_min"] == ""


def test_fetch_stats_account_for_every_headline():
    now = utc(2026, 11, 17, 12)

    def head(h, minutes_ago):
        return news.Headline(h, (now - timedelta(minutes=minutes_ago)).isoformat(), "S", h, "u")

    raw = [head("a", 10), head("b", 20), head("b", 20), head("known", 5), head("old", 500),
           news.Headline("bad", "not a date", "S", "t", "u")]
    kept = news.filter_and_dedupe(raw, ["known"], now, lookback_minutes=90)
    stats = news.fetch_stats(raw, kept, ["known"], now, 90)
    assert stats == {"fetched": 6, "outside_window": 2, "duplicates_dropped": 2, "over_cap_dropped": 0}
    assert len(kept) == 2


def test_the_scorer_records_latency_and_stop_reason_without_changing_its_result():
    good = '{"score": 3, "confidence": "low", "direction": "neutral", "key_headlines": [], "new_information": false, "reasoning": "x"}'

    def create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=good)],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7), stop_reason="end_turn")

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    result, usage = scorer.score_news([], [], client=client)
    assert result.score == 3
    assert usage.stop_reason == "end_turn"
    assert usage.latency_ms >= 0
    assert usage.as_note() == "tokens_in=11 tokens_out=7 cost_usd=0.000092 attempts=1"


# --------------------------------------------------- sheet header patching ----

def test_header_extension_only_ever_appends():
    wanted = ["a", "b", "c", "d"]
    assert sheets.header_extension(["a", "b"], wanted) == ["c", "d"]
    assert sheets.header_extension(["a", "b", "", ""], wanted) == ["c", "d"]
    assert sheets.header_extension(wanted, wanted) is None
    assert sheets.header_extension(["a", "x"], wanted) is None   # not ours to rearrange
    assert sheets.header_extension(["b", "a"], wanted) is None
    assert sheets.header_extension([], wanted) == wanted


class FakeWorksheet:
    def __init__(self, header, cols):
        self.header, self.col_count, self.updates, self.resized = header, cols, [], None

    def row_values(self, n):
        return list(self.header)

    def update(self, range_name, values):
        self.updates.append((range_name, values))

    def resize(self, cols):
        self.resized = cols
        self.col_count = cols


def make_client(ws=None, missing=False):
    import gspread
    client = object.__new__(sheets.SheetsClient)
    client._tabs = {}
    created = []

    class Spread:
        def worksheet(self, title):
            if missing:
                raise gspread.WorksheetNotFound(title)
            return ws

        def add_worksheet(self, title, rows, cols):
            created.append((title, rows, cols))
            new = FakeWorksheet([], cols)
            client._created = new
            return new

    client._sheet = Spread()
    return client, created


def test_ensure_columns_appends_only_the_missing_header_cells_and_grows_the_grid():
    old = SCHEMAS[SIGNALS][:25]
    ws = FakeWorksheet(old, cols=25)
    client, _ = make_client(ws)
    changes = client.ensure_columns(SIGNALS)
    assert ws.resized == 29
    assert ws.updates == [("Z1:AC1", [["context_version", "vol20_pct", "ma200", "event_flags"]])]
    assert changes and "context_version" in changes[0]


def test_ensure_columns_is_a_no_op_on_a_current_or_foreign_header():
    current = FakeWorksheet(SCHEMAS[SIGNALS], cols=29)
    client, _ = make_client(current)
    assert client.ensure_columns(SIGNALS) == []
    assert current.updates == []
    foreign = FakeWorksheet(["something", "else"], cols=2)
    client2, _ = make_client(foreign)
    assert client2.ensure_columns(SIGNALS) == []
    assert foreign.updates == []


def test_ensure_columns_creates_a_missing_context_tab_with_its_header():
    client, created = make_client(missing=True)
    changes = client.ensure_columns(CONTEXT)
    width = len(SCHEMAS[CONTEXT])
    assert created == [(CONTEXT, 2000, width)]
    assert changes == ["created context"]
    assert client._tabs[CONTEXT] is client._created
    assert client._tabs[CONTEXT].col_count == width


# ------------------------------------------------------ end to end in main ----

def stub_gathered(now_utc, live_price, rows):
    return {"vol20_pct": 2.31, "ma200": 88.4, "ma50": 90.0, "daily_source": "fred",
            "context_errors": "eia_stocks:connection", "event_flags": "ignored_here"}


def at(hour, minute=5):
    _FrozenDatetime.now_value = datetime(2026, 9, 16, hour, minute, tzinfo=UTC)


def run_with(monkeypatch, client, gather):
    monkeypatch.setattr(main.sheets, "open_client", lambda dry_run=False: client)
    monkeypatch.setattr(main.context, "gather", gather)
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    wire_score(monkeypatch, 8)
    assert main.run() == 0
    return client


def explode(*args, **kwargs):
    raise RuntimeError("feeds are down")


def test_a_gather_exception_leaves_c1_blank_blank_and_a_computed_event_flag(wired, monkeypatch):
    at(15)  # 11:05 New York on a Wednesday: inside the EIA window
    run_with(monkeypatch, wired, explode)

    row = wired.records(SIGNALS)[0]
    assert row["context_version"] == "c1"
    assert row["vol20_pct"] == ""
    assert row["ma200"] == ""
    assert row["event_flags"] == "eia_window"
    assert row["action"] == "buy"

    ctx = wired.records(CONTEXT)[0]
    assert ctx["run_utc"] == row["run_utc"]
    assert ctx["context_errors"] == "gather_failed:RuntimeError"
    assert ctx["event_flags"] == "eia_window"
    assert ctx["context_version"] == "c1"
    assert ctx["model_id"] == main.config.ANTHROPIC_MODEL
    assert ctx["news_new_sent"] == 1
    assert ctx["run_duration_ms"] != ""


def test_event_flag_is_blank_outside_every_window(wired, monkeypatch):
    at(17)  # 13:05 New York, after the window closed at 12:30
    run_with(monkeypatch, wired, explode)
    assert wired.records(SIGNALS)[0]["event_flags"] == ""


def test_gathered_values_reach_the_signals_row_and_the_context_tab(wired, monkeypatch):
    at(15)
    run_with(monkeypatch, wired, stub_gathered)
    row = wired.records(SIGNALS)[0]
    assert (row["context_version"], row["vol20_pct"], row["ma200"]) == ("c1", 2.31, 88.4)
    assert row["event_flags"] == "eia_window"  # always computed here, never taken from the feeds
    ctx = wired.records(CONTEXT)[0]
    assert ctx["ma50"] == 90.0
    assert ctx["context_errors"] == "eia_stocks:connection"


def test_the_first_25_columns_and_bot_1s_decision_do_not_depend_on_context(monkeypatch):
    def run_once(gather):
        client = FakeSheets()
        _FrozenDatetime.now_value = datetime(2026, 9, 16, 15, 5, tzinfo=UTC)
        monkeypatch.setattr(main, "datetime", _FrozenDatetime)
        run_with(monkeypatch, client, gather)
        return client

    with_context = run_once(stub_gathered)
    without = run_once(explode)

    first = SCHEMAS[SIGNALS][:25]
    a = with_context.records(SIGNALS)[0]
    b = without.records(SIGNALS)[0]
    assert [a[c] for c in first] == [b[c] for c in first]
    assert a["action"] == b["action"] == "buy"
    assert with_context.get_state() == without.get_state()
    assert with_context.records(sheets.HEADLINES) == without.records(sheets.HEADLINES)


def test_a_context_row_failure_cannot_touch_the_run(wired, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("row builder broke")

    monkeypatch.setattr(main.context, "build_row", broken)
    run_with(monkeypatch, wired, stub_gathered)
    assert len(wired.records(SIGNALS)) == 1
    assert wired.records(CONTEXT) == []
    assert wired.get_state()["position"] == "long"


def test_a_failing_context_tab_write_cannot_touch_the_run(wired, monkeypatch):
    real_append = wired.append

    def append(tab, rows):
        if tab == CONTEXT:
            raise RuntimeError("quota")
        return real_append(tab, rows)

    monkeypatch.setattr(wired, "append", append)
    run_with(monkeypatch, wired, stub_gathered)
    assert len(wired.records(SIGNALS)) == 1


def test_dry_run_prints_the_context_row_and_writes_nothing(monkeypatch, capsys):
    writes = []
    dry = sheets.DryRunSheets(None)
    monkeypatch.setattr(main.sheets, "open_client", lambda dry_run=False: dry)
    monkeypatch.setattr(dry, "write_state", lambda state: writes.append("state"))
    monkeypatch.setattr(main, "datetime", _FrozenDatetime)
    monkeypatch.setattr(main.context, "gather", stub_gathered)
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    wire_score(monkeypatch, 8)
    monkeypatch.setattr(main.broker, "sync", lambda *a, **k: pytest.fail("broker called"))
    _FrozenDatetime.now_value = datetime(2026, 9, 16, 15, 5, tzinfo=UTC)

    assert main.run(dry_run=True) == 0

    out = capsys.readouterr().out
    assert "[dry-run] append context:" in out
    assert '"context_version": "c1"' in out
    assert '"ma50": 90.0' in out
    assert "[dry-run] append signals:" in out
