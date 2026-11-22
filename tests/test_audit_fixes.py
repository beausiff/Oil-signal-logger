"""Behaviour tests for the audit fixes. Each one runs the real code."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from scripts import replay
from src import main, rules, scorer, sheets, stats
from src.sheets import SIGNALS, TRADES, WEEKEND_GAPS
from tests.fakes import FakeSheets
from tests.test_main import (  # noqa: F401  (wired is a fixture)
    _FrozenDatetime, fake_headline, wire_news, wire_price, wired,
)


def chi_row(year, month, day, hour, price, minute=0):
    local = datetime(year, month, day, hour, minute, tzinfo=rules.CHICAGO)
    return {
        "run_chicago": local.isoformat(),
        "run_utc": local.astimezone(timezone.utc).isoformat(),
        "brent_price": price, "score": "", "confidence": "",
    }


# ------------------------------------------------------- calendar control --

def week_without_a_friday_close_run():
    """Mon 14 Sep to Mon 21 Sep 2026, with NO run in Friday's 15:00 hour."""
    return [
        chi_row(2026, 9, 14, 10, 100.0),
        chi_row(2026, 9, 15, 10, 101.0),
        chi_row(2026, 9, 17, 10, 102.0),
        chi_row(2026, 9, 18, 12, 103.0),
        chi_row(2026, 9, 19, 12, 103.5),   # Saturday: market closed
        chi_row(2026, 9, 20, 12, 103.5),   # Sunday before the reopen
        chi_row(2026, 9, 21, 10, 104.0),
        chi_row(2026, 9, 22, 10, 105.0),
        chi_row(2026, 9, 25, 15, 106.0, minute=10),
    ]


def test_calendar_control_never_holds_across_a_weekend():
    trades = replay.calendar_only(week_without_a_friday_close_run())
    assert len(trades) == 2
    first, second = trades
    # Closed at the first weekend row, at that row's price, not carried.
    assert first["exit_utc"].startswith("2026-09-19")
    assert first["exit_price"] == 103.5
    assert first["reason"] == "friday"
    # The next week is a fresh entry, closed by the Friday close run.
    assert second["entry_price"] == 104.0
    assert second["exit_price"] == 106.0


def test_calendar_control_enters_once_a_week_even_after_a_stop():
    rows = [
        chi_row(2026, 9, 14, 10, 100.0),
        chi_row(2026, 9, 14, 12, 95.0),    # more than 3% under the entry: stopped
        chi_row(2026, 9, 15, 10, 96.0),    # same week, must not re-enter
        chi_row(2026, 9, 16, 10, 97.0),
        chi_row(2026, 9, 18, 15, 98.0, minute=10),
    ]
    stopped = replay.calendar_only(rows, use_stop=True)
    assert [t["reason"] for t in stopped] == ["stop"]
    held = replay.calendar_only(rows, use_stop=False)
    assert [t["reason"] for t in held] == ["friday"]
    assert held[0]["exit_price"] == 98.0


def test_a_sunday_evening_run_and_the_monday_are_one_week():
    rows = [
        chi_row(2026, 9, 18, 9, 100.0),              # Friday, before the entry cut off
        chi_row(2026, 9, 18, 15, 101.0, minute=5),   # Friday close run
        chi_row(2026, 9, 20, 18, 101.5),             # Sunday after the reopen
        chi_row(2026, 9, 21, 9, 102.0),              # Monday
        chi_row(2026, 9, 25, 15, 103.0, minute=5),
    ]
    trades = replay.calendar_only(rows)
    assert [t["entry_price"] for t in trades] == [100.0, 102.0]


def test_replay_exits_at_the_last_seen_price_not_the_best_price():
    rows = [
        dict(chi_row(2026, 9, 14, 10, 100.0), score="7", confidence="high"),
        chi_row(2026, 9, 14, 12, 110.0),
        dict(chi_row(2026, 9, 14, 14, ""), score="-5", confidence="high"),
    ]
    trades, _, _ = replay.replay(rows)
    assert trades[0]["exit_price"] == 110.0


def test_replay_source_comes_from_the_entry_row():
    entry = dict(chi_row(2026, 9, 14, 10, 100.0), score="7", confidence="high",
                 score_source="backfilled:2026-11-05")
    exit_ = dict(chi_row(2026, 9, 14, 12, 101.0), score="-5", confidence="high",
                 score_source="live")
    trades, _, _ = replay.replay([entry, exit_])
    assert trades[0]["source"] == "backfilled:2026-11-05"


def test_shuffled_strategy_shifts_scores_and_leaves_prices():
    rows = [dict(chi_row(2026, 9, 14 + (i // 6), 8 + (i % 6), 100.0 + i % 5),
                 score=str(i % 9), confidence="high") for i in range(60)]
    totals = replay.shuffled_strategy(rows)
    assert len(totals) == 60 - 2 * 10 + 1
    assert replay.shuffled_strategy(rows[:20]) == []


# ------------------------------------------------- circular shift test --

def autocorrelated(rng, n, rho):
    series = [0.0]
    for _ in range(n - 1):
        series.append(rho * series[-1] + rng.gauss(0, 1))
    return series


def test_circular_shift_finds_a_real_relationship():
    rng = random.Random(3)
    xs = autocorrelated(rng, 120, 0.7)
    ys = [2 * x + rng.gauss(0, 0.5) for x in xs]
    assert stats.circular_shift_test(xs, ys, rounds=300)["p"] < 0.02


def test_circular_shift_rarely_flags_independent_autocorrelated_series():
    rng = random.Random(11)
    flagged = plain_flagged = 0
    trials = 40
    for _ in range(trials):
        xs, ys = autocorrelated(rng, 150, 0.9), autocorrelated(rng, 150, 0.9)
        flagged += stats.circular_shift_test(xs, ys, rounds=150)["p"] < 0.05
        plain_flagged += stats.permutation_test(xs, ys, rounds=150)["p"] < 0.05
    assert flagged / trials <= 0.15
    assert plain_flagged > flagged   # the reason the plain shuffle is not the verdict


def test_circular_shift_refuses_small_samples():
    assert stats.circular_shift_test(list(range(29)), list(range(29))) is None


# ------------------------------------------------- scoring failure branch --

def failed_scorer(monkeypatch):
    usage = scorer.Usage(input_tokens=900, output_tokens=40, attempts=2,
                         error="scorer_invalid_json")
    monkeypatch.setattr(main.scorer, "score_news", lambda h, s, **k: (None, usage))


def open_long(**extra):
    state = {"position": "long", "entry_price": 80.0, "entry_utc": "2026-09-15T12:05:00+00:00",
             "entry_score": 7, "best_price": 80.0, "trade_id": "T-1", "last_run_utc": ""}
    state.update(extra)
    return state


def test_a_scoring_failure_still_applies_the_friday_close(wired, monkeypatch):
    _FrozenDatetime.now_value = datetime(2026, 9, 18, 20, 5, tzinfo=timezone.utc)  # Fri 15:05 CDT
    wired.state = open_long()
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 83.0)
    failed_scorer(monkeypatch)

    main.run()

    trade = wired.records(TRADES)[0]
    assert trade["exit_reason"] == "friday"
    assert trade["exit_price"] == 83.0
    assert wired.get_state()["position"] == "flat"
    assert wired.records(SIGNALS)[-1]["action"] == "close"


def test_a_scoring_failure_still_closes_late_on_a_weekend_run(wired, monkeypatch):
    _FrozenDatetime.now_value = datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc)  # Saturday
    wired.state = open_long()
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 83.0)
    failed_scorer(monkeypatch)

    main.run()

    assert wired.records(TRADES)[0]["exit_reason"] == "friday"
    assert wired.get_state()["position"] == "flat"
    # And the weekend row says so (the Friday run never made the close).
    weekend_row = wired.records(WEEKEND_GAPS)[0]
    assert weekend_row["friday_position_closed"] == "long"
    assert weekend_row["friday_exit_price"] == 83.0


def test_a_scoring_failure_with_nothing_to_close_is_an_error_and_ratchets(wired, monkeypatch):
    wired.state = open_long()
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 84.0)
    failed_scorer(monkeypatch)

    main.run()

    row = wired.records(SIGNALS)[-1]
    assert row["action"] == "error"
    assert row["position_after"] == "long"
    assert wired.get_state()["best_price"] == 84.0


def test_a_scoring_failure_applies_the_trailing_stop(wired, monkeypatch):
    wired.state = open_long(best_price=90.0)
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)   # more than 3% under 90
    failed_scorer(monkeypatch)

    main.run()

    assert wired.records(TRADES)[0]["exit_reason"] == "stop"


# --------------------------------------------------------- the alert streak --

def prior(action, headlines, **extra):
    row = {"run_utc": "2026-09-15T10:00:00+00:00", "action": action,
           "new_headline_count": headlines, "brent_price": 80.0, "score": ""}
    row.update(extra)
    return row


def test_a_quiet_run_does_not_reset_the_alert_streak(wired, monkeypatch):
    wired.append(SIGNALS, [prior("error", 5), prior("error", 5),
                           prior("flat", 0, score=3)])   # a carry row between
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    failed_scorer(monkeypatch)

    assert main.run() == 1


def test_two_failures_and_a_quiet_run_are_not_yet_an_alert(wired, monkeypatch):
    wired.append(SIGNALS, [prior("flat", 4, score=3), prior("error", 5),
                           prior("flat", 0, score=3)])
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    failed_scorer(monkeypatch)

    assert main.run() == 0


def test_a_failed_scoring_row_that_closed_a_position_still_counts():
    assert main.scoring_failed({"action": "close", "score": "", "new_headline_count": 4})
    assert not main.scoring_failed({"action": "flat", "score": "", "new_headline_count": 0})
    # Backfilled: action stays "error" but it has a score, so it is not a failure.
    assert not main.scoring_failed({"action": "error", "score": 3, "new_headline_count": 4})
    assert main.scoring_failed({"action": "error", "score": "", "new_headline_count": 4})
    assert not main.had_headlines({"new_headline_count": 0})
    assert main.had_headlines({})


# ------------------------------------------------------------ carry forward --

def test_carry_forward_with_nothing_to_carry_is_blank_not_zero():
    out = main.carry_forward([{"score": "", "confidence": ""}])
    assert out["score"] == "" and out["confidence"] == "" and out["direction"] == ""


def test_a_carried_row_is_stamped_and_a_blank_carry_does_not_trade(wired, monkeypatch):
    wire_news(monkeypatch, [])
    wire_price(monkeypatch, 80.0)
    main.run()
    row = wired.records(SIGNALS)[-1]
    assert row["score_source"] == "carried"
    assert row["score"] == ""
    assert row["action"] == "flat"


def test_a_live_row_is_stamped_live(wired, monkeypatch):
    from tests.test_main import wire_score
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    wire_score(monkeypatch, 2, "low")
    main.run()
    assert wired.records(SIGNALS)[-1]["score_source"] == "live"


# --------------------------------------------------------- the trade row --

def test_the_trade_row_records_best_price_after_the_closing_runs_update(wired, monkeypatch):
    from tests.test_main import wire_score
    wired.state = open_long()
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 82.0)
    wire_score(monkeypatch, -5, "medium")
    main.run()
    assert wired.records(TRADES)[0]["best_price"] == 82.0


def test_a_close_with_no_quote_exits_at_the_last_logged_price_not_the_best(wired, monkeypatch):
    from tests.test_main import wire_score
    wired.state = open_long(best_price=95.0)
    wired.append(SIGNALS, [prior("hold", 0, brent_price=81.0, score=2)])
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, None)
    wire_score(monkeypatch, -5, "medium")
    main.run()
    assert wired.records(TRADES)[0]["exit_price"] == 81.0


def test_a_close_with_no_quote_and_no_history_falls_back_to_the_entry(wired, monkeypatch):
    from tests.test_main import wire_score
    wired.state = open_long(best_price=95.0)
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, None)
    wire_score(monkeypatch, -5, "medium")
    main.run()
    trade = wired.records(TRADES)[0]
    assert trade["exit_price"] == 80.0


# ------------------------------------------------------- formula injection --

def test_formula_like_text_is_neutralised_and_numbers_are_not():
    assert sheets.neutralise("=IMPORTDATA(1)") == "'=IMPORTDATA(1)"
    assert sheets.neutralise("+oil jumps") == "'+oil jumps"
    assert sheets.neutralise("-Brent falls") == "'-Brent falls"
    assert sheets.neutralise("@home") == "'@home"
    assert sheets.neutralise("-3.5") == "-3.5"
    assert sheets.neutralise(-3.5) == -3.5
    assert sheets.neutralise(True) is True
    assert sheets.neutralise("Oil rises") == "Oil rises"


def test_the_write_paths_neutralise():
    row = sheets._row_from_dict(sheets.HEADLINES, {"title": "=1+1", "hash": "abc"})
    assert row[sheets.SCHEMAS[sheets.HEADLINES].index("title")] == "'=1+1"


# ------------------------------------------------------ registered variants --

def scored(row, score, conf="high"):
    return dict(row, score=str(score), confidence=conf)


def test_a_reversal_is_ignored_without_use_reversal_but_the_locked_replay_takes_it():
    rows = [
        scored(chi_row(2026, 9, 14, 10, 100.0), 7),
        scored(chi_row(2026, 9, 14, 12, 101.0), -5),
        chi_row(2026, 9, 15, 10, 102.0),
    ]
    locked, _, _ = replay.replay(rows)
    assert [t["reason"] for t in locked] == ["reversal"]
    trades, position, _ = replay.replay_variant(rows, use_reversal=False)
    assert trades == [] and position.is_open
    assert position.best_price == 102.0   # still ratcheting while held


def test_without_reversal_the_stop_and_the_friday_close_still_fire():
    stop_rows = [
        scored(chi_row(2026, 9, 14, 10, 100.0), 7),
        scored(chi_row(2026, 9, 14, 12, 110.0), -5),   # reversal ignored, best 110
        chi_row(2026, 9, 14, 14, 100.0),               # more than 3% off the best
    ]
    trades, _, _ = replay.replay_variant(stop_rows, use_reversal=False)
    assert [t["reason"] for t in trades] == ["stop"]

    friday_rows = [
        scored(chi_row(2026, 9, 14, 10, 100.0), 7),
        scored(chi_row(2026, 9, 15, 10, 100.5), -5),
        chi_row(2026, 9, 18, 15, 101.0, minute=5),
    ]
    trades, _, _ = replay.replay_variant(friday_rows, use_reversal=False)
    assert [t["reason"] for t in trades] == ["friday"]


def entry_rows():
    return [scored(chi_row(2026, 9, 14, 10, 100.0), 4),
            chi_row(2026, 9, 18, 15, 101.0, minute=5)]


def test_the_entry_override_takes_effect_and_config_is_restored():
    locked = replay.replay(entry_rows())[0]
    assert locked == []
    before = (replay.config.ENTRY_LONG_SCORE, replay.config.ENTRY_SHORT_SCORE)
    trades, _, _ = replay.replay_variant(entry_rows(), entry=4)
    assert len(trades) == 1
    assert (replay.config.ENTRY_LONG_SCORE, replay.config.ENTRY_SHORT_SCORE) == before


def test_the_entry_override_is_restored_even_if_the_replay_raises(monkeypatch):
    before = (replay.config.ENTRY_LONG_SCORE, replay.config.ENTRY_SHORT_SCORE)

    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(replay.rules, "decide", boom)
    with pytest.raises(RuntimeError):
        replay.replay_variant(entry_rows(), entry=4)
    assert (replay.config.ENTRY_LONG_SCORE, replay.config.ENTRY_SHORT_SCORE) == before


def test_replay_variant_with_defaults_equals_replay():
    rows = [scored(chi_row(2026, 9, 14, 10, 100.0), 7),
            scored(chi_row(2026, 9, 14, 12, 101.0), -5),
            chi_row(2026, 9, 15, 10, 102.0)]
    assert replay.replay_variant(rows) == replay.replay(rows)
