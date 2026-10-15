"""Week four analysis. Reads the sheet, prints statistics, writes nothing.

    python -m scripts.analyse

Runs where the credentials already are, so only the summary numbers travel.
"""

from __future__ import annotations

import re
import sys
from typing import List, Optional

from src import config, stats
from src.outcomes import to_float
from src.sheets import SIGNALS, WEEKEND_GAPS, SheetsClient

HORIZONS = ["1h", "4h", "24h", "72h"]
COST = re.compile(r"cost_usd=([0-9.]+)")


def truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def pairs(rows: List[dict], horizon: str):
    """(score, forward move) for every row that has both."""
    out = []
    for row in rows:
        score = to_float(row.get("score"))
        move = to_float(row.get("move_%s_pct" % horizon))
        if score is not None and move is not None:
            out.append((score, move))
    return out


def correlate(rows: List[dict], horizon: str, label: str) -> None:
    data = pairs(rows, horizon)
    if len(data) < 4:
        print("  %-28s n=%-4d too few to correlate" % (label, len(data)))
        return
    xs = [d[0] for d in data]
    ys = [d[1] for d in data]
    r = stats.pearson(xs, ys)
    if r is None:
        print("  %-28s n=%-4d undefined (no variance)" % (label, len(data)))
        return
    neff = stats.effective_n(xs, ys)
    lo, hi = stats.fisher_ci(r, int(neff))
    straddles = "crosses zero" if lo <= 0 <= hi else "excludes zero"
    print(
        "  %-28s n=%-4d n_eff=%-6.1f r=%+.3f  95%% CI [%+.3f, %+.3f]  %s"
        % (label, len(data), neff, r, lo, hi, straddles)
    )


def score_distribution(rows: List[dict]) -> None:
    counts = {}
    for row in rows:
        score = to_float(row.get("score"))
        if score is not None:
            counts[int(score)] = counts.get(int(score), 0) + 1
    total = sum(counts.values())
    print("  score  count  share")
    for score in sorted(counts):
        n = counts[score]
        bar = "#" * max(1, round(40 * n / total))
        print("  %+5d  %5d  %5.1f%%  %s" % (score, n, 100 * n / total, bar))
    print("  total %5d" % total)


def threshold_sweep(rows: List[dict], require_confidence: bool) -> None:
    gate = "medium/high confidence only" if require_confidence else "any confidence"
    print("\n  Entry threshold sweep, %s, market open rows only" % gate)
    print("  %-6s %-7s %-22s %-22s" % ("thresh", "entries", "24h signed return", "72h signed return"))

    for threshold in (3, 4, 5, 6):
        entries = []
        for row in rows:
            if not truthy(row.get("market_open")):
                continue
            score = to_float(row.get("score"))
            if score is None or abs(score) < threshold:
                continue
            if require_confidence and str(row.get("confidence", "")).strip() not in config.ENTRY_CONFIDENCES:
                continue
            entries.append(row)

        cells = []
        for horizon in ("24h", "72h"):
            signed = []
            for row in entries:
                score = to_float(row.get("score"))
                move = to_float(row.get("move_%s_pct" % horizon))
                if score and move is not None:
                    signed.append(move if score > 0 else -move)
            d = stats.describe(signed)
            if d["n"] == 0:
                cells.append("%-22s" % "no data")
            else:
                cells.append(
                    "%-22s" % ("mean %+.3f%%  hit %.0f%% (n=%d)" % (d["mean"], 100 * d["hit"], d["n"]))
                    if d["hit"] is not None
                    else "%-22s" % ("mean %+.3f%% (n=%d)" % (d["mean"], d["n"]))
                )
        print("  %-6s %-7d %s %s" % ("+/-%d" % threshold, len(entries), cells[0], cells[1]))


def split(rows: List[dict], field: str, buckets) -> None:
    for name, test in buckets:
        subset = [r for r in rows if test(r)]
        correlate(subset, "24h", "%s (24h)" % name)
        correlate(subset, "72h", "%s (72h)" % name)


def main() -> int:
    client = SheetsClient()
    rows = client.records(SIGNALS)
    weekends = client.records(WEEKEND_GAPS)

    print("=" * 78)
    print("OIL SIGNAL LOGGER, WEEK FOUR ANALYSIS")
    print("=" * 78)
    print("\nsignals rows: %d" % len(rows))
    print("market open rows: %d" % sum(1 for r in rows if truthy(r.get("market_open"))))
    print("rules versions: %s" % sorted({str(r.get("rules_version", "")) for r in rows}))

    total_cost = 0.0
    for row in rows:
        match = COST.search(str(row.get("notes", "")))
        if match:
            total_cost += float(match.group(1))
    print("total anthropic spend: $%.2f" % total_cost)

    print("\n--- 1. SCORE DISTRIBUTION ---")
    score_distribution(rows)

    print("\n--- 2. DOES THE SCORE PREDICT THE MOVE? ---")
    for horizon in HORIZONS:
        correlate(rows, horizon, "all rows, %s" % horizon)
    print("\n  market open rows only:")
    open_rows = [r for r in rows if truthy(r.get("market_open"))]
    for horizon in HORIZONS:
        correlate(open_rows, horizon, "market open, %s" % horizon)

    print("\n--- 3. THRESHOLD SWEEP (post hoc, no rule was changed) ---")
    threshold_sweep(rows, require_confidence=True)
    threshold_sweep(rows, require_confidence=False)

    print("\n--- 4. BY CONFIDENCE ---")
    split(rows, "confidence", [
        ("low", lambda r: str(r.get("confidence", "")).strip() == "low"),
        ("medium", lambda r: str(r.get("confidence", "")).strip() == "medium"),
        ("high", lambda r: str(r.get("confidence", "")).strip() == "high"),
    ])

    print("\n--- 5. BY NEW INFORMATION ---")
    split(rows, "new_information", [
        ("new_information TRUE", lambda r: truthy(r.get("new_information"))),
        ("new_information FALSE", lambda r: not truthy(r.get("new_information"))),
    ])

    print("\n--- 6. WEEKENDS ---")
    for w in weekends:
        print(
            "  %s  fri %s -> sun %s -> mon %s | gap %s%% | implied %s | matched %s | signal pnl %s%%"
            % (
                w.get("weekend_start_date"),
                w.get("friday_last_price"),
                w.get("sunday_reopen_price"),
                w.get("monday_12utc_price") or "-",
                w.get("gap_pct") or "-",
                w.get("weekend_implied_action"),
                w.get("gap_direction_matched_weekend_score"),
                w.get("weekend_signal_pnl_pct") or "-",
            )
        )

    print("\n--- 7. NEAR MISSES AND ACTIONS ---")
    # Three rows scored +6, the entry threshold, yet nothing was ever bought.
    # This section exists to say exactly why, row by row, rather than leaving
    # the reader to infer it from a zero in the trades tab.
    interesting = [
        r for r in rows
        if (to_float(r.get("score")) or 0) >= config.ENTRY_LONG_SCORE - 1
        or (to_float(r.get("score")) or 0) <= config.ENTRY_SHORT_SCORE + 1
        or str(r.get("action", "")).strip() not in ("hold", "")
    ]
    print("  rows at |score| >= %d, or with a non hold action: %d"
          % (abs(config.ENTRY_SHORT_SCORE) - 1, len(interesting)))
    for r in interesting:
        print("  %s  score %+3s  %-6s  open=%-5s  action=%-5s  pos=%-5s  %s"
              % (str(r.get("run_utc"))[:16], r.get("score"), r.get("confidence"),
                 r.get("market_open"), r.get("action"), r.get("position_after"),
                 str(r.get("notes"))[:60]))

    print("\n--- 8. TRADES ---")
    from src.sheets import TRADES
    trades = client.records(TRADES)
    print("  closed paper trades: %d" % len(trades))
    for t in trades:
        print("  %s" % t)

    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
