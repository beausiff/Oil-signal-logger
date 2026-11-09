"""Ask the data different questions than the one the experiment was built on.

    python -m scripts.hypotheses

The brief asked whether a news score predicts the DIRECTION of Brent. Three
independent tests say it does not. But direction over a few hours is close to
a coin flip, and news may be better at saying that something is about to
happen than at saying which way it goes.

So this tests three other questions on rows already in the sheet, costing
nothing and waiting for nobody:

  1. Does the score predict the SIZE of the move, ignoring sign?
  2. Does it predict direction only on the hours that actually moved?
  3. Does the raw HEADLINE COUNT do just as well as the model's score?

Three is the control that matters. If counting headlines predicts volatility
as well as reading them does, the model is adding nothing over a word count
and no amount of prompt tuning will change that.
"""

from __future__ import annotations

import sys
from typing import List, Optional

from src import stats
from src.outcomes import to_float
from src.sheets import SIGNALS, SheetsClient
from src import sheets

HORIZONS = ["1h", "4h", "24h", "72h"]
BIG_MOVE_PCT = 1.0


def truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def paired(rows: List[dict], left: str, horizon: str, absolute: bool):
    """(predictor, outcome) for rows that have both."""
    out = []
    for row in rows:
        x = to_float(row.get(left))
        y = to_float(row.get("move_%s_pct" % horizon))
        if x is None or y is None:
            continue
        out.append((x, abs(y) if absolute else y))
    return out


def report(data, label: str, rounds: int = 2000) -> Optional[float]:
    if len(data) < 12:
        print("  %-34s n=%-4d too few" % (label, len(data)))
        return None
    xs = [d[0] for d in data]
    ys = [d[1] for d in data]
    r = stats.pearson(xs, ys)
    if r is None:
        print("  %-34s n=%-4d no variance" % (label, len(data)))
        return None
    result = stats.permutation_test(xs, ys, rounds=rounds)
    p = result["p"] if result else float("nan")
    print("  %-34s n=%-4d r=%+.3f  p=%.3f  %s"
          % (label, len(data), r, p,
             "SIGNAL" if p < 0.05 else "chance"))
    return p


def main() -> int:
    client = sheets.open_client(dry_run=False)
    rows = client.records(SIGNALS)
    rows.sort(key=lambda r: str(r.get("run_utc", "")))
    print("=" * 78)
    print("ALTERNATIVE HYPOTHESES, on %d rows already logged" % len(rows))
    print("=" * 78)

    print("\n--- 1. DOES THE SCORE PREDICT THE SIZE OF THE MOVE? ---")
    print("  Sign thrown away. A high score meaning 'something is happening'")
    print("  would show up here even with no directional skill at all.")
    for horizon in HORIZONS:
        report(paired(rows, "score", horizon, absolute=True), "|score->|move|| %s" % horizon)

    print("\n  same, using the absolute score, since a -6 is also a loud hour:")
    loud = []
    for horizon in HORIZONS:
        data = [(abs(x), y) for x, y in paired(rows, "score", horizon, absolute=True)]
        loud.append(report(data, "abs(score)->|move| %s" % horizon))

    print("\n--- 2. DOES IT PREDICT DIRECTION ON THE HOURS THAT MOVED? ---")
    print("  Ordinary hours are noise. If a signal exists it should show on")
    print("  the hours where something actually happened (|move| > %.1f%%)." % BIG_MOVE_PCT)
    for horizon in HORIZONS:
        data = [(x, y) for x, y in paired(rows, "score", horizon, absolute=False)
                if abs(y) >= BIG_MOVE_PCT]
        report(data, "score->move, big moves %s" % horizon)

    print("\n--- 3. THE CONTROL: DOES COUNTING BEAT READING? ---")
    print("  Headline count needs no model and costs nothing. If it matches")
    print("  the score, the model is adding nothing over a word count.")
    for horizon in HORIZONS:
        score_p = report(paired(rows, "score", horizon, absolute=True),
                         "  model score -> |move| %s" % horizon)
        count_p = report(paired(rows, "new_headline_count", horizon, absolute=True),
                         "  headline count -> |move| %s" % horizon)
        if score_p is not None and count_p is not None:
            verdict = "model wins" if score_p < count_p else "counting wins or ties"
            print("  %-34s %s" % ("    ->", verdict))

    print("\n--- 4. DOES 'NEW INFORMATION' MARK A LOUDER HOUR? ---")
    for horizon in HORIZONS:
        fresh = [abs(to_float(r.get("move_%s_pct" % horizon)) or 0.0) for r in rows
                 if truthy(r.get("new_information")) and to_float(r.get("move_%s_pct" % horizon)) is not None]
        stale = [abs(to_float(r.get("move_%s_pct" % horizon)) or 0.0) for r in rows
                 if not truthy(r.get("new_information")) and to_float(r.get("move_%s_pct" % horizon)) is not None]
        if len(fresh) < 10 or len(stale) < 10:
            print("  %-34s too few" % ("new_information %s" % horizon))
            continue
        print("  %-34s new=%.3f%% (n=%d)  stale=%.3f%% (n=%d)  diff=%+.3f%%"
              % ("new_information %s" % horizon, stats.mean(fresh), len(fresh),
                 stats.mean(stale), len(stale), stats.mean(fresh) - stats.mean(stale)))

    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
