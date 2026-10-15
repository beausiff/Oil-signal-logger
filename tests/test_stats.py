"""The analysis arithmetic, checked against cases with known answers."""

import math

import pytest

from src import stats


def test_perfect_positive_correlation():
    assert stats.pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)


def test_perfect_negative_correlation():
    assert stats.pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)


def test_no_correlation_is_exactly_zero():
    """Symmetric by construction, so the answer is 0 and not approximately 0."""
    assert stats.pearson([1, 2, 3, 4], [1, 2, 2, 1]) == pytest.approx(0.0)


def test_a_constant_series_has_no_defined_correlation():
    """The whole point of the week four caveat: a flat predictor predicts nothing."""
    assert stats.pearson([2, 2, 2, 2], [1, 5, 3, 9]) is None


def test_too_few_points_returns_none():
    assert stats.pearson([1, 2], [3, 4]) is None


def test_fisher_interval_brackets_the_estimate():
    lo, hi = stats.fisher_ci(0.3, 100)
    assert lo < 0.3 < hi
    assert lo > 0  # a real effect at n=100


def test_a_weak_correlation_at_small_n_crosses_zero():
    lo, hi = stats.fisher_ci(0.15, 20)
    assert lo < 0 < hi


def test_lag1_detects_a_repeating_series():
    assert stats.lag1([1, 1, 1, 1, 5, 5, 5, 5]) > 0.3


def test_lag1_of_an_alternating_series_is_negative():
    assert stats.lag1([1, -1, 1, -1, 1, -1, 1, -1]) < 0


def test_effective_n_shrinks_when_both_series_are_sticky():
    sticky = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6]
    assert stats.effective_n(sticky, sticky) < len(sticky)


def test_effective_n_never_exceeds_the_row_count():
    noisy = [1, -1, 1, -1, 1, -1, 1, -1, 1, -1]
    assert stats.effective_n(noisy, noisy) <= len(noisy)


def test_hit_rate_counts_only_non_zero_moves():
    assert stats.hit_rate([1.0, 1.0, -1.0, 0.0]) == pytest.approx(2 / 3)


def test_hit_rate_of_all_zeroes_is_undefined():
    assert stats.hit_rate([0.0, 0.0]) is None


def test_describe_on_empty():
    assert stats.describe([])["n"] == 0


def test_describe_reports_mean_and_hit():
    d = stats.describe([2.0, -1.0, 1.0])
    assert d["n"] == 3
    assert d["mean"] == pytest.approx(2 / 3)
    assert d["hit"] == pytest.approx(2 / 3)
