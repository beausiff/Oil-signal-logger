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


def pending_updates(records: Sequence[dict], now_utc: datetime) -> Dict[int, Dict[str, object]]:
    """Work out every cell that can be filled right now, keyed by sheet row."""
    series = build_price_series(records)
    updates: Dict[int, Dict[str, object]] = {}

    for row in records:
        run_at = parse_utc(row.get("run_utc", ""))
        if run_at is None:
            continue
        base_price = to_float(row.get("brent_price"))

        for label, hours in config.OUTCOME_HORIZONS_HOURS.items():
            price_col = "price_%s" % label
            move_col = "move_%s_pct" % label
            if str(row.get(price_col, "")).strip():
                continue

            target = run_at + timedelta(hours=hours)
            if target > now_utc:
                continue

            found = nearest_price(series, target)
            if found is None:
                continue

            cells = updates.setdefault(int(row["_row"]), {})
            cells[price_col] = found
            if base_price:
                cells[move_col] = round((found - base_price) / base_price * 100.0, 4)

    return updates


def backfill(client, now_utc: datetime) -> int:
    """Apply every available update. Returns the number of rows touched."""
    from .sheets import SIGNALS

    records = client.records(SIGNALS)
    updates = pending_updates(records, now_utc)
    for row_number, cells in sorted(updates.items()):
        client.update_cells(SIGNALS, row_number, cells)
    return len(updates)
