"""Rebuild the trend, volatility and event inputs for race rows logged before them.

    rows = backfill_context.backfill(rows)

The logger started writing context_version, vol20_pct, ma200 and event_flags on
25 Nov 2026 at 14:05 UTC. Bots 7 to 16 read those columns, so without them they
could only race from that hour. This fills the same columns for the earlier v2
rows, in memory only, the way the logger would have at the time:

* the daily Brent series is backtest/brent_daily.csv, cut to dates before the
  row's own date (FRED lags about 5 days, so at race time it ended where the
  file ends anyway), then extended with this bot's own logged prices up to and
  including that row, exactly as src.context.assemble does;
* event_flags comes from src.context.event_flags, which needs no network.

Nothing after a row is used to fill it. Filled rows are marked
context_version "c1-replayed" so they can always be told from logged ones.
Rows that already carry a context_version are left untouched.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from src import context, outcomes
from src.outcomes import parse_utc, to_float

REPLAYED = "c1-replayed"


def _is_race(row: dict) -> bool:
    return str(row.get("rules_version", "")).strip().startswith("v2")


def backfill(rows: Sequence[dict], daily: Optional[List[Tuple[str, float]]] = None) -> List[dict]:
    """A copy of `rows` with context filled on v2 rows that have none."""
    rows = sorted((dict(r) for r in rows), key=lambda r: str(r.get("run_utc", "")))
    if not any(_is_race(r) and not str(r.get("context_version") or "").strip() for r in rows):
        return rows
    if daily is None:
        daily = context.load_daily_file()
    meetings = context.load_opec_dates()
    for i, row in enumerate(rows):
        if not _is_race(row) or str(row.get("context_version") or "").strip():
            continue
        now = parse_utc(row.get("run_utc", ""))
        if now is None:
            continue
        cutoff = now.date().isoformat()
        known = [(d, p) for d, p in daily if d < cutoff]
        logged = outcomes.build_price_series(rows[: i + 1])
        live = to_float(row.get("brent_price"))
        series, _ = context.extend_daily_series(known, logged, live, now)
        metrics = context.daily_metrics(series, live)
        row["context_version"] = REPLAYED
        row["vol20_pct"] = metrics.get("vol20_pct", "")
        row["ma200"] = metrics.get("ma200", "")
        row["event_flags"] = context.event_flags(now, meetings)
    return rows
