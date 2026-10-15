"""Plain statistics for the week four analysis. No numpy, no scipy.

Kept separate from the analysis script so the arithmetic can be unit tested
without a Google credential.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Correlation coefficient, or None when it is undefined."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def fisher_ci(r: float, n: int, z: float = 1.96) -> Tuple[float, float]:
    """95% confidence interval for a correlation, via the Fisher transform.

    Reported instead of a p-value: an interval that straddles zero says
    "indistinguishable from nothing" without inviting a significance ritual.
    """
    if n < 4 or abs(r) >= 1:
        return (-1.0, 1.0)
    zr = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    return (math.tanh(zr - z * se), math.tanh(zr + z * se))


def lag1(xs: Sequence[float]) -> float:
    """Lag one autocorrelation."""
    n = len(xs)
    if n < 3:
        return 0.0
    m = mean(xs)
    num = sum((xs[i] - m) * (xs[i + 1] - m) for i in range(n - 1))
    den = sum((x - m) ** 2 for x in xs)
    return num / den if den > 0 else 0.0


def effective_n(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Sample size discounted for autocorrelation.

    Consecutive hourly observations are not independent: the same news drives
    several rows and the price barely moves between them. Treating n as the
    row count would overstate the evidence, so the headline number is this one.
    """
    n = len(xs)
    if n < 4:
        return float(n)
    a, b = lag1(xs), lag1(ys)
    factor = (1 - a * b) / (1 + a * b) if (1 + a * b) != 0 else 1.0
    return max(2.0, min(float(n), n * factor))


def hit_rate(returns: Sequence[float]) -> Optional[float]:
    """Share of signalled moves that went the right way. Zeroes excluded."""
    moves = [r for r in returns if r != 0]
    if not moves:
        return None
    return sum(1 for r in moves if r > 0) / len(moves)


def describe(returns: Sequence[float]) -> dict:
    if not returns:
        return {"n": 0, "mean": None, "hit": None}
    return {"n": len(returns), "mean": mean(returns), "hit": hit_rate(returns)}
