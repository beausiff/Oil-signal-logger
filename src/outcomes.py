"""Backfill forward price moves onto older `signals` rows.

Each run, any row with a blank price_1h/4h/24h/72h whose target time has passed
is filled from the nearest logged Brent price within 20 minutes of that target.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from . import config


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


def build_price_series(records: Sequence[dict]) -> List[Tuple[datetime, float]]:
    """Every logged Brent price, oldest first, keyed on the run timestamp."""
    series: List[Tuple[datetime, float]] = []
    for row in records:
        when = parse_utc(row.get("run_utc", ""))
        price = to_float(row.get("brent_price"))
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


def pending_targets(records: Sequence[dict], now_utc: datetime) -> List[dict]:
    """Every outcome cell that is blank and whose horizon has already passed."""
    pending: List[dict] = []
    for row in records:
        run_at = parse_utc(row.get("run_utc", ""))
        if run_at is None:
            continue
        base_price = to_float(row.get("brent_price"))

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


def backfill(client, now_utc: datetime, fetch_range=None) -> int:
    """Fill forward price columns. Returns the number of rows touched.

    Our own logged prices are used first, because they cost nothing. Anything
    they cannot reach, because a scheduled run was dropped, is looked up from
    the price API in a single range call covering every outstanding horizon.
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

        earliest = min(item["target"] for item in unresolved)
        window = timedelta(minutes=config.OUTCOME_MATCH_WINDOW_MINUTES)
        floor = now_utc - timedelta(days=config.OUTCOME_API_MAX_RANGE_DAYS)
        start = max(earliest - window, floor)

        if start <= now_utc:
            fetched = fetch_range(start, now_utc)
            if fetched:
                extra, _ = resolve(unresolved, _merge(series, fetched))
                for row_number, cells in extra.items():
                    updates.setdefault(row_number, {}).update(cells)

    for row_number, cells in sorted(updates.items()):
        client.update_cells(SIGNALS, row_number, cells)
    return len(updates)
