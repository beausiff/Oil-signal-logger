"""The backfill writes into a live dataset, so what it must NOT touch matters
as much as what it fills in."""

from __future__ import annotations

import pytest

from scripts import backfill_scores as bf
from src import config, scorer


def signal(run_utc, action="error", score="", **extra):
    row = {
        "run_utc": run_utc, "action": action, "score": score,
        "position_after": "flat", "brent_price": "100.0", "_row": 2,
    }
    row.update(extra)
    return row


# ------------------------------------------------------- what to rescore --

def test_error_rows_with_no_score_are_targets():
    assert bf.needs_score(signal("2026-10-17T09:27"))


def test_a_scored_row_is_never_rescored():
    assert not bf.needs_score(signal("2026-10-17T09:27", score="3"))


def test_a_healthy_row_is_never_rescored():
    assert not bf.needs_score(signal("2026-10-17T09:27", action="flat"))


def test_an_already_backfilled_row_is_not_done_twice():
    row = signal("2026-10-17T09:27", score="2", score_source="backfilled:2026-11-05")
    assert not bf.needs_score(row)


# -------------------------------------------------------------- grouping --

def test_headlines_are_grouped_by_their_run():
    class Fake:
        def records(self, tab):
            return [
                {"run_utc": "A", "title": "one"},
                {"run_utc": "B", "title": "two"},
                {"run_utc": "A", "title": "three"},
            ]

    grouped = bf.headlines_by_run(Fake())
    assert len(grouped["A"]) == 2
    assert len(grouped["B"]) == 1


def test_headline_rows_become_scorer_input():
    row = {"title": "OPEC cuts", "source": "Reuters", "published_utc": "x",
           "url": "u", "hash": "h", "run_utc": "r"}
    out = bf.as_headline_dict(row)
    assert out["title"] == "OPEC cuts"
    assert "run_utc" not in out


# ---------------------------------------------------------- the contract --

def test_the_patch_never_touches_the_trade_record():
    """The rules did not run on these scores. Saying they did would be a lie."""
    source = open("scripts/backfill_scores.py").read()
    patch_block = source.split("patch = {")[1].split("}")[0]
    assert '"action"' not in patch_block
    assert '"position_after"' not in patch_block


def test_every_backfilled_row_is_stamped():
    source = open("scripts/backfill_scores.py").read()
    patch_block = source.split("patch = {")[1].split("}")[0]
    assert '"score_source": stamp' in patch_block


def test_score_source_is_a_real_column():
    from src.sheets import SCHEMAS, SIGNALS
    assert "score_source" in SCHEMAS[SIGNALS]


def test_score_source_is_appended_not_inserted():
    """Inserting it would shift every column of every existing row."""
    from src.sheets import SCHEMAS, SIGNALS
    assert SCHEMAS[SIGNALS][-1] == "score_source"
    assert SCHEMAS[SIGNALS][:4] == [
        "run_utc", "run_chicago", "rules_version", "market_open"]
