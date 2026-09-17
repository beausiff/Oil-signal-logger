"""The rules are the experiment, so they are the thing that gets tested."""

from dataclasses import replace
from datetime import datetime

import pytest

from src import config, rules
from src.rules import FLAT, Position


def chi(year, month, day, hour, minute=5):
    return datetime(year, month, day, hour, minute, tzinfo=rules.CHICAGO)


LONG = Position(side="long", entry_price=80.0, entry_utc="2026-09-16T12:05:00+00:00",
                entry_score=7, best_price=80.0, trade_id="T-1")
SHORT = Position(side="short", entry_price=80.0, entry_utc="2026-09-16T12:05:00+00:00",
                 entry_score=-7, best_price=80.0, trade_id="T-2")

WEDNESDAY_NOON = chi(2026, 9, 16, 12)


# ------------------------------------------------------------ market hours --

@pytest.mark.parametrize(
    "moment,expected",
    [
        (chi(2026, 9, 16, 12), True),    # Wednesday midday
        (chi(2026, 9, 16, 16, 30), False),  # daily break
        (chi(2026, 9, 16, 17, 5), True),   # after the break
        (chi(2026, 9, 18, 15), True),    # Friday before the close
        (chi(2026, 9, 18, 16, 5), False),  # Friday after the close
        (chi(2026, 9, 19, 12), False),   # Saturday
        (chi(2026, 9, 20, 12), False),   # Sunday before reopen
        (chi(2026, 9, 20, 17, 5), True),   # Sunday reopen
    ],
)
def test_market_hours(moment, expected):
    assert rules.is_market_open(moment) is expected


# ------------------------------------------------------------------ entry --

def test_long_entry_at_threshold():
    decision = rules.decide(score=6, confidence="medium", position=FLAT,
                            now_chicago=WEDNESDAY_NOON, price=80.0)
    assert decision.action == "buy"
    assert decision.position_after.side == "long"
    assert decision.position_after.entry_price == 80.0
    assert decision.position_after.best_price == 80.0


def test_short_entry_at_threshold():
    decision = rules.decide(score=-6, confidence="high", position=FLAT,
                            now_chicago=WEDNESDAY_NOON, price=80.0)
    assert decision.action == "sell"
    assert decision.position_after.side == "short"


def test_no_entry_below_threshold():
    decision = rules.decide(score=5, confidence="high", position=FLAT,
                            now_chicago=WEDNESDAY_NOON, price=80.0)
    assert decision.action == "flat"
    assert decision.position_after.side == "flat"


def test_no_entry_on_low_confidence():
    decision = rules.decide(score=9, confidence="low", position=FLAT,
                            now_chicago=WEDNESDAY_NOON, price=80.0)
    assert decision.action == "flat"


def test_no_entry_without_a_price():
    decision = rules.decide(score=9, confidence="high", position=FLAT,
                            now_chicago=WEDNESDAY_NOON, price=None)
    assert decision.action == "flat"
    assert "no_price" in decision.notes


def test_one_position_at_a_time():
    decision = rules.decide(score=9, confidence="high", position=LONG,
                            now_chicago=WEDNESDAY_NOON, price=81.0)
    assert decision.action == "hold"
    assert decision.position_after.side == "long"


# ------------------------------------------------------------ trailing stop --

def test_best_price_ratchets_up_for_a_long():
    moved = rules.update_best_price(LONG, 85.0)
    assert moved.best_price == 85.0
    assert rules.update_best_price(moved, 82.0).best_price == 85.0


def test_best_price_ratchets_down_for_a_short():
    moved = rules.update_best_price(SHORT, 75.0)
    assert moved.best_price == 75.0
    assert rules.update_best_price(moved, 78.0).best_price == 75.0


def test_trailing_stop_closes_a_long():
    position = replace(LONG, best_price=90.0)
    decision = rules.decide(score=0, confidence="high", position=position,
                            now_chicago=WEDNESDAY_NOON, price=87.2)  # 3.1% off the high
    assert decision.action == "close"
    assert decision.exit_reason == "stop"


def test_trailing_stop_does_not_fire_inside_the_band():
    position = replace(LONG, best_price=90.0)
    decision = rules.decide(score=0, confidence="high", position=position,
                            now_chicago=WEDNESDAY_NOON, price=87.4)  # 2.9% off the high
    assert decision.action == "hold"


def test_trailing_stop_closes_a_short():
    position = replace(SHORT, best_price=70.0)
    decision = rules.decide(score=0, confidence="high", position=position,
                            now_chicago=WEDNESDAY_NOON, price=72.2)  # 3.1% against
    assert decision.action == "close"
    assert decision.exit_reason == "stop"


# ---------------------------------------------------------- reversal exits --

def test_long_closes_on_reversal():
    decision = rules.decide(score=-3, confidence="medium", position=LONG,
                            now_chicago=WEDNESDAY_NOON, price=80.0)
    assert decision.action == "close"
    assert decision.exit_reason == "reversal"


def test_short_closes_on_reversal():
    decision = rules.decide(score=3, confidence="medium", position=SHORT,
                            now_chicago=WEDNESDAY_NOON, price=80.0)
    assert decision.action == "close"
    assert decision.exit_reason == "reversal"


def test_reversal_needs_no_confidence_floor():
    decision = rules.decide(score=-4, confidence="low", position=LONG,
                            now_chicago=WEDNESDAY_NOON, price=80.0)
    assert decision.action == "close"


# -------------------------------------------------------------- Friday rule --

def test_no_new_entries_after_friday_ten():
    decision = rules.decide(score=9, confidence="high", position=FLAT,
                            now_chicago=chi(2026, 9, 18, 10), price=80.0)
    assert decision.action == "flat"
    assert decision.notes == "friday_no_new_entries"


def test_entries_still_allowed_friday_morning():
    decision = rules.decide(score=9, confidence="high", position=FLAT,
                            now_chicago=chi(2026, 9, 18, 9), price=80.0)
    assert decision.action == "buy"


def test_friday_forced_close_on_the_last_run():
    decision = rules.decide(score=9, confidence="high", position=LONG,
                            now_chicago=chi(2026, 9, 18, 15), price=81.0)
    assert decision.action == "close"
    assert decision.exit_reason == "friday"


def test_friday_forced_close_beats_the_trailing_stop():
    position = replace(LONG, best_price=90.0)
    decision = rules.decide(score=0, confidence="high", position=position,
                            now_chicago=chi(2026, 9, 18, 15), price=80.0)
    assert decision.exit_reason == "friday"


# ----------------------------------------------------------- market closed --

def test_market_closed_takes_no_action():
    decision = rules.decide(score=10, confidence="high", position=FLAT,
                            now_chicago=chi(2026, 9, 19, 12), price=80.0)
    assert decision.action == "flat"
    assert decision.position_after.side == "flat"
    assert decision.notes == "market_closed"


def test_daily_break_takes_no_action():
    decision = rules.decide(score=10, confidence="high", position=FLAT,
                            now_chicago=chi(2026, 9, 16, 16, 30), price=80.0)
    assert decision.action == "flat"


def test_weekend_news_never_opens_or_closes():
    for hour in range(0, 24):
        for day in (19, 20):
            moment = chi(2026, 9, day, hour)
            if rules.is_market_open(moment):
                continue
            for score in (-10, -6, 0, 6, 10):
                decision = rules.decide(score=score, confidence="high", position=FLAT,
                                        now_chicago=moment, price=80.0)
                assert decision.action == "flat"
                assert decision.position_after.side == "flat"


def test_position_is_carried_if_the_market_shuts_on_one():
    decision = rules.decide(score=-10, confidence="high", position=LONG,
                            now_chicago=chi(2026, 9, 19, 12), price=70.0)
    assert decision.action == "flat"
    assert decision.position_after.side == "long"


# --------------------------------------------------------------------- pnl --

def test_long_pnl_deducts_the_spread():
    usd, pct = rules.pnl("long", 80.0, 82.0)
    assert usd == pytest.approx(2.0 - config.SPREAD_USD_PER_BBL)
    assert pct == pytest.approx((2.0 - config.SPREAD_USD_PER_BBL) / 80.0 * 100, rel=1e-6)


def test_short_pnl_deducts_the_spread():
    usd, _ = rules.pnl("short", 80.0, 78.0)
    assert usd == pytest.approx(2.0 - config.SPREAD_USD_PER_BBL)


def test_losing_trade_is_negative():
    usd, pct = rules.pnl("long", 80.0, 79.0)
    assert usd < 0 and pct < 0
