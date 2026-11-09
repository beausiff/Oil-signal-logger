"""A run that scores nothing used to exit 0 and look healthy. It ran that way
for 19 days."""

from __future__ import annotations

from src import config


def rows(*actions):
    return [{"action": a} for a in actions]


def should_fail(all_rows):
    recent = all_rows[-config.ALERT_AFTER_CONSECUTIVE_ERRORS:]
    return len(recent) == config.ALERT_AFTER_CONSECUTIVE_ERRORS and all(
        str(r.get("action", "")).strip() == "error" for r in recent
    )


def test_one_bad_hour_is_not_an_alert():
    assert not should_fail(rows("flat", "flat", "error"))


def test_two_bad_hours_are_not_an_alert():
    assert not should_fail(rows("flat", "error", "error"))


def test_three_in_a_row_fails_the_job():
    assert should_fail(rows("flat", "error", "error", "error"))


def test_a_recovery_clears_it():
    assert not should_fail(rows("error", "error", "flat"))


def test_a_brand_new_sheet_does_not_alert():
    assert not should_fail(rows("error"))


def test_the_threshold_is_configurable_not_hardcoded():
    source = open("src/main.py").read()
    assert "config.ALERT_AFTER_CONSECUTIVE_ERRORS" in source


def test_the_job_writes_everything_before_it_fails():
    """The alert must be the last thing, or a failing run loses its data."""
    source = open("src/main.py").read()
    assert source.index("client.append(SIGNALS") < source.index("FAILING THE JOB")
    assert source.index("outcomes.backfill") < source.index("FAILING THE JOB")


# ------------------------------------------------- the price feed as well --

def priced(*values):
    return [{"brent_price": v} for v in values]


def should_fail_on_price(all_rows):
    recent = all_rows[-config.ALERT_AFTER_CONSECUTIVE_BLANK_PRICES:]
    return len(recent) == config.ALERT_AFTER_CONSECUTIVE_BLANK_PRICES and all(
        str(r.get("brent_price", "")).strip() == "" for r in recent
    )


def test_an_occasional_blank_price_is_tolerated():
    """The vendor rate limits. A few gaps are normal and must not cry wolf."""
    assert not should_fail_on_price(priced("", "100.1", "", "", "100.2", ""))


def test_a_dead_price_feed_fails_the_job():
    assert should_fail_on_price(priced("", "", "", "", "", ""))


def test_one_good_price_clears_it():
    assert not should_fail_on_price(priced("", "", "", "", "", "97.3"))


def test_the_price_alert_is_slacker_than_the_scoring_one():
    """Blank prices are commoner than dead keys, so it waits longer."""
    assert config.ALERT_AFTER_CONSECUTIVE_BLANK_PRICES > config.ALERT_AFTER_CONSECUTIVE_ERRORS


def test_both_alerts_run_after_every_write():
    source = open("src/main.py").read()
    assert source.index("client.append(SIGNALS") < source.index("logged no Brent price")
