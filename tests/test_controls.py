"""The controls exist to try to kill the result. They are only worth having if
they cannot accidentally flatter it."""

from __future__ import annotations

import pytest

from scripts import replay
from src import stats


# ------------------------------------------------- correlation permutation --

def test_a_real_relationship_is_found():
    xs = list(range(40))
    ys = [i * 2 for i in range(40)]
    assert stats.permutation_test(xs, ys, rounds=400)["p"] < 0.01


def test_noise_is_not_mistaken_for_signal():
    xs = list(range(40))
    ys = [(i * 7) % 11 for i in range(40)]
    assert stats.permutation_test(xs, ys, rounds=400)["p"] > 0.2


def test_p_is_never_exactly_zero():
    """A finite number of shuffles cannot prove impossibility."""
    out = stats.permutation_test(list(range(50)), [i * 3 for i in range(50)], rounds=200)
    assert out["p"] > 0


def test_too_little_data_returns_nothing_rather_than_a_number():
    assert stats.permutation_test([1, 2, 3], [1, 2, 3], rounds=100) is None


def test_the_same_seed_gives_the_same_answer():
    a = stats.permutation_test(list(range(30)), [(i * 5) % 7 for i in range(30)], rounds=200)
    b = stats.permutation_test(list(range(30)), [(i * 5) % 7 for i in range(30)], rounds=200)
    assert a["p"] == b["p"]


# ------------------------------------------------------- calendar control --

def test_the_calendar_control_reads_no_scores():
    """If it read the score column it would not be a control. Prose about the
    score is fine; reading the field is not."""
    body = open("scripts/replay.py").read().split("def calendar_only")[1].split("def shuffled_strategy")[0]
    for read in ('get("score"', '["score"]', 'get("confidence"', '["confidence"]'):
        assert read not in body, read


def test_the_shuffle_keeps_prices_where_they_are():
    """Shuffling prices too would test nothing: it would just be a new market."""
    body = open("scripts/replay.py").read().split("def shuffled_strategy")[1].split("def main")[0]
    assert "brent_price" not in body
    assert '"score"' in body and '"confidence"' in body


def test_replay_imports_the_locked_rules_rather_than_restating_them():
    source = open("scripts/replay.py").read()
    assert "rules.decide(" in source
    assert "ENTRY_LONG_SCORE =" not in source


def test_the_replay_never_writes():
    source = open("scripts/replay.py").read()
    for forbidden in ("client.append", "update_cells", "write_state", "ensure_tabs"):
        assert forbidden not in source
