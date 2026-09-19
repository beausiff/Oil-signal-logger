from datetime import datetime, timezone

import pytest

from src import rules, weekend
from src.sheets import WEEKEND_GAPS
from tests.fakes import FakeSheets


def chi(day, hour, minute=5):
    return datetime(2026, 9, day, hour, minute, tzinfo=rules.CHICAGO)


def signal(day, hour, score, headlines=""):
    moment = chi(day, hour)
    return {
        "run_utc": moment.astimezone(timezone.utc).isoformat(),
        "run_chicago": moment.isoformat(),
        "market_open": "false",
        "score": score,
        "key_headlines": headlines,
    }


# ------------------------------------------------------------ weekend key --

@pytest.mark.parametrize(
    "day,hour,expected",
    [
        (18, 15, "2026-09-18"),   # Friday
        (19, 3, "2026-09-18"),    # Saturday
        (20, 18, "2026-09-18"),   # Sunday after reopen
        (21, 7, "2026-09-18"),    # Monday morning
        (16, 12, None),           # Wednesday has no weekend window
    ],
)
def test_weekend_key(day, hour, expected):
    assert weekend.weekend_key(chi(day, hour)) == expected


# ---------------------------------------------------------- row creation --

def test_friday_close_creates_the_row_with_a_closed_position():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 80.0, 4, "long", 80.5)

    rows = client.records(WEEKEND_GAPS)
    assert len(rows) == 1
    assert rows[0]["weekend_start_date"] == "2026-09-18"
    assert rows[0]["friday_last_price"] == 80.0
    assert rows[0]["friday_position_closed"] == "long"
    assert rows[0]["friday_exit_price"] == 80.5


def test_friday_close_with_no_open_position():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 80.0, 1, "none", None)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["friday_position_closed"] == "none"
    assert row["friday_exit_price"] == ""


def test_friday_close_is_idempotent():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 80.0, 4, "none", None)
    weekend.on_friday_close(client, chi(18, 15), 80.4, 5, "none", None)

    rows = client.records(WEEKEND_GAPS)
    assert len(rows) == 1
    assert rows[0]["friday_last_price"] == 80.4


# ------------------------------------------------------------ aggregation --

def test_weekend_runs_aggregate_scores():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 80.0, 2, "none", None)
    rows = [signal(19, 9, 3, "Tanker hit"), signal(19, 14, 7, "Hormuz threat"), signal(20, 2, 1)]
    weekend.on_weekend_run(client, chi(20, 2), rows)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["weekend_run_count"] == 3
    assert row["weekend_max_score"] == 7
    assert row["weekend_min_score"] == 1
    assert row["weekend_avg_score"] == pytest.approx(11 / 3, abs=0.01)
    assert row["weekend_implied_action"] == "buy"
    assert "Hormuz threat" in row["weekend_key_headlines"]


def test_weekend_runs_with_no_scores_stay_flat():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 80.0, 0, "none", None)
    weekend.on_weekend_run(client, chi(19, 9), [{"score": "", "key_headlines": ""}])

    row = client.records(WEEKEND_GAPS)[0]
    assert row["weekend_implied_action"] == "flat"
    assert row["weekend_avg_score"] == ""


def test_weekend_run_creates_the_row_if_friday_was_missed():
    client = FakeSheets()
    weekend.on_weekend_run(client, chi(19, 9), [signal(19, 9, -8)])

    row = client.records(WEEKEND_GAPS)[0]
    assert row["weekend_start_date"] == "2026-09-18"
    assert row["weekend_implied_action"] == "sell"


@pytest.mark.parametrize(
    "max_score,min_score,expected",
    [(7, 0, "buy"), (0, -7, "sell"), (5, -5, "flat"), (6, -6, "buy")],
)
def test_implied_action_thresholds(max_score, min_score, expected):
    assert weekend.implied_action(max_score, min_score) == expected


# ------------------------------------------------------------- completion --

def _weekend_with_scores():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 80.0, 2, "long", 80.0)
    weekend.on_weekend_run(client, chi(20, 2), [signal(19, 14, 8)])
    return client


def test_sunday_reopen_fills_the_gap():
    client = _weekend_with_scores()
    weekend.on_open_run(client, chi(20, 18), chi(20, 18).astimezone(timezone.utc), 82.4)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["sunday_reopen_price"] == 82.4
    assert row["gap_pct"] == pytest.approx(3.0, abs=0.01)
    assert row["gap_direction_matched_weekend_score"] is True
    assert row["held_through_pnl_pct"] == pytest.approx(3.0, abs=0.01)


def test_gap_against_the_weekend_score_is_marked_false():
    client = _weekend_with_scores()
    weekend.on_open_run(client, chi(20, 18), chi(20, 18).astimezone(timezone.utc), 78.0)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["gap_direction_matched_weekend_score"] is False


def test_monday_noon_utc_completes_the_row():
    client = _weekend_with_scores()
    weekend.on_open_run(client, chi(20, 18), chi(20, 18).astimezone(timezone.utc), 82.4)

    monday = datetime(2026, 9, 21, 12, 5, tzinfo=timezone.utc)
    weekend.on_open_run(client, monday.astimezone(rules.CHICAGO), monday, 83.2)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["monday_12utc_price"] == 83.2
    assert row["held_through_pnl_monday_pct"] == pytest.approx(4.0, abs=0.01)
    assert row["weekend_signal_pnl_pct"] == pytest.approx(4.0, abs=0.01)
    assert all(str(row[col]).strip() != "" for col in (
        "weekend_start_date", "friday_last_price", "sunday_reopen_price",
        "monday_12utc_price", "gap_pct", "weekend_signal_pnl_pct",
    ))


def test_monday_is_not_completed_before_noon_utc():
    client = _weekend_with_scores()
    weekend.on_open_run(client, chi(20, 18), chi(20, 18).astimezone(timezone.utc), 82.4)

    early = datetime(2026, 9, 21, 9, 5, tzinfo=timezone.utc)
    weekend.on_open_run(client, early.astimezone(rules.CHICAGO), early, 83.2)

    assert client.records(WEEKEND_GAPS)[0]["monday_12utc_price"] == ""


def test_missing_prices_leave_the_row_untouched():
    client = _weekend_with_scores()
    weekend.on_open_run(client, chi(20, 18), chi(20, 18).astimezone(timezone.utc), None)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["sunday_reopen_price"] == ""
    assert row["gap_pct"] == ""


def test_no_friday_position_leaves_held_through_blank():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 80.0, 2, "none", None)
    weekend.on_open_run(client, chi(20, 18), chi(20, 18).astimezone(timezone.utc), 82.4)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["sunday_reopen_price"] == 82.4
    assert row["held_through_pnl_pct"] == ""


def test_short_held_through_is_inverted():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 80.0, -7, "short", 80.0)
    weekend.on_open_run(client, chi(20, 18), chi(20, 18).astimezone(timezone.utc), 78.4)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["held_through_pnl_pct"] == pytest.approx(2.0, abs=0.01)


# ------------------------------- the Friday anchor without a Friday run --

def friday_signal(hour, price, score):
    """A signals row on Friday 2026-09-18 at the given Chicago hour."""
    moment = chi(18, hour)
    return {
        "run_utc": moment.astimezone(timezone.utc).isoformat(),
        "run_chicago": moment.isoformat(),
        "market_open": "true",
        "brent_price": price,
        "score": score,
        "key_headlines": "",
    }


def test_the_friday_anchor_comes_from_the_last_pre_close_row():
    history = [
        friday_signal(9, 100.0, 1),
        friday_signal(14, 103.0, 4),   # last before the 16:00 close
        friday_signal(17, 109.0, 9),   # after the close, must be ignored
    ]
    snap = weekend.friday_snapshot(history, "2026-09-18")
    assert snap == {"friday_last_price": 103.0, "friday_last_score": 4.0}


def test_the_anchor_is_backfilled_when_the_close_run_was_dropped():
    """Exactly what happened on 2026-09-18: no run landed in 15:00-16:00."""
    client = FakeSheets()
    history = [friday_signal(9, 100.0, 1), friday_signal(14, 103.0, 4)]

    # The row gets created by a weekend run, with no Friday snapshot.
    weekend.on_weekend_run(client, chi(19, 9), [signal(19, 9, 2)])
    assert client.records(WEEKEND_GAPS)[0]["friday_last_price"] == ""

    # A later weekend run, now with history, fills the anchor in.
    weekend.on_weekend_run(client, chi(19, 14), [signal(19, 14, 2)], all_signal_rows=history)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["friday_last_price"] == 103.0
    assert row["friday_last_score"] == 4.0


def test_an_existing_anchor_is_never_overwritten():
    client = FakeSheets()
    weekend.on_friday_close(client, chi(18, 15), 105.0, 6, "long", 105.0)
    weekend.on_weekend_run(
        client, chi(19, 9), [signal(19, 9, 2)],
        all_signal_rows=[friday_signal(14, 103.0, 4)],
    )
    assert client.records(WEEKEND_GAPS)[0]["friday_last_price"] == 105.0


def test_no_friday_rows_leaves_the_anchor_blank():
    assert weekend.friday_snapshot([], "2026-09-18") == {}
    assert weekend.friday_snapshot([friday_signal(17, 109.0, 9)], "2026-09-18") == {}


def test_the_anchor_completes_the_gap_maths():
    """With the anchor present, the reopen can compute a gap."""
    client = FakeSheets()
    weekend.on_weekend_run(
        client, chi(19, 9), [signal(19, 9, 8)],
        all_signal_rows=[friday_signal(14, 100.0, 4)],
    )
    weekend.on_open_run(client, chi(20, 18), chi(20, 18).astimezone(timezone.utc), 103.0)

    row = client.records(WEEKEND_GAPS)[0]
    assert row["gap_pct"] == pytest.approx(3.0, abs=0.01)
    assert row["gap_direction_matched_weekend_score"] is True
