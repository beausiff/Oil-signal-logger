"""Does the model agree with itself?

    python -m scripts.reliability --sample 30

Nobody checks this, and it sets a ceiling on everything else. If the model
scores the same headlines +4 one day and +1 the next, then its correlation
with ANY outcome is bounded by how well it correlates with itself. Chasing a
signal above that ceiling was never possible, and no prompt tuning fixes it.

The arithmetic: a measure with reliability k cannot correlate with a perfect
outcome above sqrt(k). So test-retest of 0.6 caps any real correlation at
about 0.77, and a noisier outcome like an hourly oil return drags it far
lower again.

Writes nothing. The original scores in the sheet are left exactly as they are.
"""

from __future__ import annotations

import argparse
import math
import sys
from typing import List

from src import config, scorer, sheets, stats
from src.outcomes import to_float
from src.sheets import HEADLINES, SIGNALS


def headlines_by_run(client) -> dict:
    out = {}
    for row in client.records(HEADLINES):
        out.setdefault(str(row.get("run_utc", "")).strip(), []).append({
            "title": row.get("title", ""), "source": row.get("source", ""),
            "published_utc": row.get("published_utc", ""), "url": row.get("url", ""),
            "hash": row.get("hash", ""),
        })
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score the same hours twice and compare.")
    parser.add_argument("--sample", type=int, default=30, help="how many hours to rescore")
    args = parser.parse_args(argv)

    client = sheets.open_client(dry_run=False)
    rows = client.records(SIGNALS)
    rows.sort(key=lambda r: str(r.get("run_utc", "")))
    heads = headlines_by_run(client)

    scored = [r for r in rows
              if to_float(r.get("score")) is not None and heads.get(str(r.get("run_utc", "")).strip())]
    if len(scored) < 10:
        print("not enough scored rows with headlines")
        return 0

    # Spread the sample across the whole period rather than taking a run of
    # consecutive hours, which would share their headlines and flatter the result.
    step = max(1, len(scored) // args.sample)
    sample = scored[::step][:args.sample]
    print("rescoring %d hours, spread evenly across %d scored rows" % (len(sample), len(scored)))

    originals, repeats = [], []
    spend = 0.0
    for row in sample:
        run_utc = str(row.get("run_utc", "")).strip()
        index = rows.index(row)
        context = rows[max(0, index - config.CONTEXT_SIGNAL_ROWS):index]
        again, usage = scorer.score_news(heads[run_utc], context)
        if usage:
            spend += usage.cost_usd
        if again is None:
            print("  %s  rescore failed" % run_utc[:16])
            continue
        first = to_float(row.get("score"))
        originals.append(first)
        repeats.append(float(again.score))
        print("  %s  was %+d  now %+d  %s"
              % (run_utc[:16], int(first), again.score,
                 "same" if int(first) == again.score else "DIFFERENT"))

    if len(originals) < 8:
        print("\ntoo few successful rescores to judge")
        return 0

    r = stats.pearson(originals, repeats)
    exact = sum(1 for a, b in zip(originals, repeats) if int(a) == int(b))
    within_one = sum(1 for a, b in zip(originals, repeats) if abs(a - b) <= 1)
    gaps = [abs(a - b) for a, b in zip(originals, repeats)]

    print("\n  n=%d   spend $%.2f" % (len(originals), spend))
    print("  exact agreement:   %d/%d (%.0f%%)" % (exact, len(originals), 100.0 * exact / len(originals)))
    print("  within one point:  %d/%d (%.0f%%)" % (within_one, len(originals), 100.0 * within_one / len(originals)))
    print("  mean gap:          %.2f points" % stats.mean(gaps))
    if r is not None:
        print("  test retest r:     %+.3f" % r)
        if r > 0:
            print("  ceiling this puts on any correlation: %.2f" % math.sqrt(r))
            print("  (a measure cannot correlate with anything better than it does with itself)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
