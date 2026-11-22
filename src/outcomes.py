"""Backfill forward price moves onto older `signals` rows.

Each run, any row with a blank price_1h/4h/24h/72h whose target time has passed
is filled from the nearest logged Brent price within 20 minutes of that target.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from . import config, rules


def parse_utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_float(value) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def quote_age_minutes(row: dict) -> Optional[float]:
    """Minutes between the run and the moment its Brent quote was taken.

    None when the row does not say. A closed market logs the last quote, which
    can be a day old, so run_utc alone is not when that price was true.
    """
    run_at = parse_utc(row.get("run_utc", ""))
    quoted_at = parse_utc(row.get("price_time_utc", ""))
    if run_at is None or quoted_at is None:
        return None
    return (run_at - quoted_at).total_seconds() / 60.0


def build_price_series(records: Sequence[dict]) -> List[Tuple[datetime, float]]:
    """Every logged Brent price, oldest first, keyed on the run timestamp.

    A row whose quote was stale is left out: it would stand in for a price at a
    time it was not true. Rows that do not record a quote time are kept.
    """
    series: List[Tuple[datetime, float]] = []
    for row in records:
        when = parse_utc(row.get("run_utc", ""))
        price = to_float(row.get("brent_price"))
        age = quote_age_minutes(row)
        if age is not None and age > config.OUTCOME_MATCH_WINDOW_MINUTES:
            continue
        if when is not None and price is not None:
            series.append((when, price))
    series.sort(key=lambda item: item[0])
    return series


def nearest_price(
    series: Sequence[Tuple[datetime, float]],
    target: datetime,
    window_minutes: int = config.OUTCOME_MATCH_WINDOW_MINUTES,
) -> Optional[float]:
    best: Optional[Tuple[timedelta, float]] = None
    window = timedelta(minutes=window_minutes)
    for when, price in series:
        gap = abs(when - target)
        if gap <= window and (best is None or gap < best[0]):
            best = (gap, price)
    return best[1] if best else None


def is_clean(row: dict, horizon_hours: float) -> bool:
    """Is this row's forward move a real one?

    Market open at the run, a fresh quote (within 30 minutes), and market open
    at the horizon. Closed market rows log the last quote, so their moves are
    fake zeros or mislabelled; the analyses report on this sample first.
    """
    run_at = parse_utc(row.get("run_utc", ""))
    age = quote_age_minutes(row)
    if run_at is None or age is None or abs(age) > 30:
        return False
    if not rules.is_market_open(rules.to_chicago(run_at)):
        return False
    return rules.is_market_open(rules.to_chicago(run_at + timedelta(hours=horizon_hours)))


def pending_targets(records: Sequence[dict], now_utc: datetime) -> List[dict]:
    """Every outcome cell that is blank and whose horizon has already passed."""
    pending: List[dict] = []
    for row in records:
        run_at = parse_utc(row.get("run_utc", ""))
        if run_at is None:
            continue
        base_price = to_float(row.get("brent_price"))
        if not base_price:
            # No base to measure from: filling the price alone would leave the
            # move blank for good, because a filled cell is never revisited.
            continue

        for label, hours in config.OUTCOME_HORIZONS_HOURS.items():
            if str(row.get("price_%s" % label, "")).strip():
                continue
            target = run_at + timedelta(hours=hours)
            if target > now_utc:
                continue
            pending.append(
                {
                    "row": int(row["_row"]),
                    "label": label,
                    "target": target,
                    "base_price": base_price,
                }
            )
    return pending


def resolve(
    pending: Sequence[dict], series: Sequence[Tuple[datetime, float]]
) -> Tuple[Dict[int, Dict[str, object]], List[dict]]:
    """Fill what the series can reach. Returns (updates, still unresolved)."""
    updates: Dict[int, Dict[str, object]] = {}
    unresolved: List[dict] = []

    for item in pending:
        found = nearest_price(series, item["target"])
        if found is None:
            unresolved.append(item)
            continue
        cells = updates.setdefault(item["row"], {})
        cells["price_%s" % item["label"]] = found
        base = item["base_price"]
        if base:
            cells["move_%s_pct" % item["label"]] = round((found - base) / base * 100.0, 4)

    return updates, unresolved


def pending_updates(records: Sequence[dict], now_utc: datetime) -> Dict[int, Dict[str, object]]:
    """What can be filled from our own logged prices alone."""
    series = build_price_series(records)
    updates, _ = resolve(pending_targets(records, now_utc), series)
    return updates


def _merge(
    a: Sequence[Tuple[datetime, float]], b: Sequence[Tuple[datetime, float]]
) -> List[Tuple[datetime, float]]:
    merged = {when: price for when, price in a}
    merged.update({when: price for when, price in b})
    return sorted(merged.items(), key=lambda item: item[0])


def target_windows(
    unresolved: Sequence[dict], now_utc: datetime, window_minutes: int = None
) -> List[Tuple[datetime, datetime]]:
    """Narrow windows around each outstanding target, overlaps merged.

    The price API caps a page at 100 points, so a single wide sweep comes back
    truncated and the oldest targets never resolve. Asking around each target
    keeps every response small enough to be complete.
    """
    if window_minutes is None:
        window_minutes = config.OUTCOME_MATCH_WINDOW_MINUTES
    span = timedelta(minutes=window_minutes)
    floor = now_utc - timedelta(days=config.OUTCOME_API_MAX_RANGE_DAYS)

    windows: List[Tuple[datetime, datetime]] = []
    for target in sorted({item["target"] for item in unresolved}):
        start = max(target - span, floor)
        end = min(target + span, now_utc)
        if end <= start:
            continue
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows


def backfill(client, now_utc: datetime, fetch_range=None) -> int:
    """Fill forward price columns. Returns the number of rows touched.

    Our own logged prices are used first, because they cost nothing. Anything
    they cannot reach, because a scheduled run was dropped, is looked up from
    the price API in narrow windows around the outstanding targets.
    """
    from .sheets import SIGNALS

    records = client.records(SIGNALS)
    pending = pending_targets(records, now_utc)
    if not pending:
        return 0

    series = build_price_series(records)
    updates, unresolved = resolve(pending, series)

    if unresolved and config.OUTCOME_USE_PRICE_API:
        if fetch_range is None:
            from .price import fetch_brent_range as fetch_range

        fetched: List[Tuple[datetime, float]] = []
        windows = target_windows(unresolved, now_utc)
        # Oldest first: those are the ones about to fall out of the range the
        # API will still serve.
        for start, end in windows[: config.OUTCOME_API_CALLS_PER_RUN]:
            fetched.extend(fetch_range(start, end))

        if len(windows) > config.OUTCOME_API_CALLS_PER_RUN:
            print(
                "outcome backfill: %d windows outstanding, doing %d this run"
                % (len(windows), config.OUTCOME_API_CALLS_PER_RUN)
            )

        if fetched:
            extra, _ = resolve(unresolved, _merge(series, fetched))
            for row_number, cells in extra.items():
                updates.setdefault(row_number, {}).update(cells)

    for row_number, cells in sorted(updates.items()):
        client.update_cells(SIGNALS, row_number, cells)
    return len(updates)
