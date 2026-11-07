"""GitHub drops most scheduled runs, so the workflow asks six times an hour.
These check that the extra attempts cost nothing and never skip a real hour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import config
from src.main import minutes_since_last_run

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def state(minutes_ago=None):
    if minutes_ago is None:
        return {"last_run_utc": ""}
    return {"last_run_utc": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def would_skip(gap, force=False):
    return not force and gap is not None and gap < config.MIN_MINUTES_BETWEEN_RUNS


def test_a_fresh_sheet_always_runs():
    assert minutes_since_last_run(state(), NOW) is None
    assert not would_skip(None)


def test_a_run_ten_minutes_ago_is_skipped():
    assert would_skip(minutes_since_last_run(state(10), NOW))


def test_a_run_an_hour_ago_proceeds():
    assert not would_skip(minutes_since_last_run(state(60), NOW))


def test_the_boundary_lets_the_hour_through():
    """55 not 60, or a run at :03 blocks the one at :57 and the hour is lost."""
    assert config.MIN_MINUTES_BETWEEN_RUNS < 60
    assert not would_skip(minutes_since_last_run(state(56), NOW))


def test_a_long_outage_proceeds():
    assert not would_skip(minutes_since_last_run(state(60 * 24 * 19), NOW))


def test_force_overrides_the_guard():
    assert not would_skip(minutes_since_last_run(state(1), NOW), force=True)


def test_a_rubbish_timestamp_does_not_block_the_run():
    assert minutes_since_last_run({"last_run_utc": "not a date"}, NOW) is None


def test_the_guard_runs_before_anything_is_spent():
    """A skipped attempt must not fetch news, price or a score."""
    source = open("src/main.py").read()
    guard = source.index("minimum gap is")
    for costly in ("news.collect", "price_client.fetch_brent", "scorer.score_news"):
        assert source.index(costly) > guard, costly


def test_the_schedule_fires_more_than_once_an_hour():
    workflow = open(".github/workflows/hourly.yml").read()
    cron = workflow.split('- cron: "')[1].split('"')[0]
    assert len(cron.split()[0].split(",")) >= 4
