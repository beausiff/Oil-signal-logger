"""Weekend gap analysis.

Runs continue hourly on Saturday and Sunday: news is fetched, scored and logged
with market_open = false and action = flat. No position is ever opened or
closed while the market is shut. This module builds one `weekend_gaps` row per
weekend, opened at the Friday close and completed after Monday 12:00 UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Sequence

from . import config
from .outcomes import to_float

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)

_OFFSET_TO_FRIDAY = {FRIDAY: 0, SATURDAY: 1, SUNDAY: 2, MONDAY: 3}


def weekend_key(now_chicago: datetime) -> Optional[str]:
    """ISO date of the Friday that starts the weekend window `now` falls in."""
    offset = _OFFSET_TO_FRIDAY.get(now_chicago.weekday())
    if offset is None:
        return None
    return (now_chicago.date() - timedelta(days=offset)).isoformat()


def implied_action(max_score: Optional[float], min_score: Optional[float]) -> str:
    """Entry thresholds applied to the weekend's extremes."""
    if max_score is not None and max_score >= config.ENTRY_LONG_SCORE:
        return "buy"
    if min_score is not None and min_score <= config.ENTRY_SHORT_SCORE:
        return "sell"
    return "flat"


def pct_move(from_price: Optional[float], to_price: Optional[float]) -> Optional[float]:
    if not from_price or to_price is None:
        return None
    return round((to_price - from_price) / from_price * 100.0, 4)


def directional_pnl_pct(side: str, entry: Optional[float], exit_price: Optional[float]) -> Optional[float]:
    """Percent a long or short would have made between two prices."""
    if side not in ("long", "short") or not entry or exit_price is None:
        return None
    move = (exit_price - entry) if side == "long" else (entry - exit_price)
    return round(move / entry * 100.0, 4)


def gap_matched(gap: Optional[float], action: str) -> Optional[bool]:
    """Did the Monday open move the way the weekend score implied?

    A `flat` implication is counted as correct when the gap stayed under 0.5%.
    """
    if gap is None:
        return None
    if action == "buy":
        return gap > 0
    if action == "sell":
        return gap < 0
    return abs(gap) < 0.5


def summarise_weekend_signals(rows: Sequence[dict]) -> Dict[str, object]:
    """Aggregate the closed market `signals` rows belonging to one weekend."""
    scores: List[float] = []
    headlines: List[str] = []
    for row in rows:
        score = to_float(row.get("score"))
        if score is not None:
            scores.append(score)
        text = str(row.get("key_headlines", "")).strip()
        if text:
            headlines.extend(part.strip() for part in text.split(" | ") if part.strip())

    if not scores:
        return {
            "weekend_run_count": len(rows),
            "weekend_avg_score": "",
            "weekend_max_score": "",
            "weekend_min_score": "",
            "weekend_key_headlines": " | ".join(dict.fromkeys(headlines))[:2000],
            "weekend_implied_action": "flat",
        }

    max_score, min_score = max(scores), min(scores)
    return {
        "weekend_run_count": len(rows),
        "weekend_avg_score": round(sum(scores) / len(scores), 3),
        "weekend_max_score": max_score,
        "weekend_min_score": min_score,
        "weekend_key_headlines": " | ".join(dict.fromkeys(headlines))[:2000],
        "weekend_implied_action": implied_action(max_score, min_score),
    }


def open_row(
    weekend_start: str,
    friday_last_price: Optional[float],
    friday_last_score: Optional[float],
    friday_position_closed: str = "none",
    friday_exit_price: Optional[float] = None,
) -> dict:
    """The row written at the Friday close."""
    return {
        "weekend_start_date": weekend_start,
        "friday_last_price": "" if friday_last_price is None else friday_last_price,
        "friday_last_score": "" if friday_last_score is None else friday_last_score,
        "friday_position_closed": friday_position_closed,
        "friday_exit_price": "" if friday_exit_price is None else friday_exit_price,
        "weekend_run_count": 0,
        "weekend_avg_score": "",
        "weekend_max_score": "",
        "weekend_min_score": "",
        "weekend_key_headlines": "",
        "weekend_implied_action": "flat",
        "sunday_reopen_price": "",
        "monday_12utc_price": "",
        "gap_pct": "",
        "gap_direction_matched_weekend_score": "",
        "held_through_pnl_pct": "",
        "held_through_pnl_monday_pct": "",
        "weekend_signal_pnl_pct": "",
    }


def reopen_updates(row: dict, sunday_reopen_price: Optional[float]) -> Dict[str, object]:
    """Fields completed on the first run after the Sunday reopen."""
    if sunday_reopen_price is None:
        return {}

    friday_last = to_float(row.get("friday_last_price"))
    friday_exit = to_float(row.get("friday_exit_price"))
    side = str(row.get("friday_position_closed", "none")).strip()
    gap = pct_move(friday_last, sunday_reopen_price)
    action = str(row.get("weekend_implied_action", "flat")).strip() or "flat"
    matched = gap_matched(gap, action)

    updates: Dict[str, object] = {"sunday_reopen_price": sunday_reopen_price}
    if gap is not None:
        updates["gap_pct"] = gap
    if matched is not None:
        updates["gap_direction_matched_weekend_score"] = matched
    held = directional_pnl_pct(side, friday_exit, sunday_reopen_price)
    if held is not None:
        updates["held_through_pnl_pct"] = held
    return updates


def monday_updates(row: dict, monday_price: Optional[float]) -> Dict[str, object]:
    """Fields completed on the first run after Monday 12:00 UTC."""
    if monday_price is None:
        return {}

    friday_last = to_float(row.get("friday_last_price"))
    friday_exit = to_float(row.get("friday_exit_price"))
    side = str(row.get("friday_position_closed", "none")).strip()
    action = str(row.get("weekend_implied_action", "flat")).strip() or "flat"

    updates: Dict[str, object] = {"monday_12utc_price": monday_price}
    held = directional_pnl_pct(side, friday_exit, monday_price)
    if held is not None:
        updates["held_through_pnl_monday_pct"] = held

    if action == "buy":
        signal_pnl = pct_move(friday_last, monday_price)
    elif action == "sell":
        signal_pnl = directional_pnl_pct("short", friday_last, monday_price)
    else:
        signal_pnl = 0.0
    if signal_pnl is not None:
        updates["weekend_signal_pnl_pct"] = signal_pnl
    return updates


def is_past_monday_noon_utc(now_utc: datetime) -> bool:
    return now_utc.weekday() == MONDAY and now_utc.hour >= config.MONDAY_COMPLETION_HOUR_UTC


# ------------------------------------------------------------- orchestration --

def on_friday_close(client, now_chicago, price, score, closed_side, exit_price) -> None:
    """Create or refresh the weekend row at the Friday close run."""
    from .sheets import WEEKEND_GAPS

    key = weekend_key(now_chicago)
    if key is None:
        return
    existing = client.find_row(WEEKEND_GAPS, "weekend_start_date", key)
    row = open_row(key, price, score, closed_side or "none", exit_price)
    if existing is None:
        client.append(WEEKEND_GAPS, [row])
    else:
        client.update_cells(WEEKEND_GAPS, int(existing["_row"]), row)


def friday_snapshot(all_signal_rows: Sequence[dict], weekend_start: str) -> Dict[str, object]:
    """The last price and score logged before the Friday close.

    Taken from the signals history rather than from whichever run happened to
    land between 15:00 and 16:00 Chicago. That run is frequently dropped, and
    without this anchor gap_pct and weekend_signal_pnl_pct cannot be computed
    at all.
    """
    from .outcomes import parse_utc

    best = None
    for row in all_signal_rows:
        when = parse_utc(row.get("run_chicago", ""))
        if when is None:
            continue
        local = when.astimezone(ZoneInfo(config.MARKET_TZ))
        if local.date().isoformat() != weekend_start:
            continue
        if local.hour >= config.FRIDAY_CLOSE_HOUR:
            continue
        if best is None or local > best[0]:
            best = (local, row)

    if best is None:
        return {}

    row = best[1]
    snapshot: Dict[str, object] = {}
    price = to_float(row.get("brent_price"))
    score = to_float(row.get("score"))
    if price is not None:
        snapshot["friday_last_price"] = price
    if score is not None:
        snapshot["friday_last_score"] = score
    return snapshot


def on_weekend_run(client, now_chicago, weekend_signal_rows, all_signal_rows=None) -> None:
    """Refresh the weekend aggregates on each closed market run."""
    from .sheets import WEEKEND_GAPS

    key = weekend_key(now_chicago)
    if key is None:
        return
    existing = client.find_row(WEEKEND_GAPS, "weekend_start_date", key)
    summary = summarise_weekend_signals(weekend_signal_rows)

    if existing is None:
        row = open_row(key, None, None)
        row.update(summary)
        if all_signal_rows:
            row.update(friday_snapshot(all_signal_rows, key))
        client.append(WEEKEND_GAPS, [row])
        return

    # Backfill the Friday anchor if the close run never fired.
    if all_signal_rows and not str(existing.get("friday_last_price", "")).strip():
        summary.update(friday_snapshot(all_signal_rows, key))
    client.update_cells(WEEKEND_GAPS, int(existing["_row"]), summary)


def reopen_utc(weekend_start: str) -> datetime:
    """Sunday 17:00 Chicago, as UTC, for the weekend starting that Friday."""
    friday = date.fromisoformat(weekend_start)
    sunday = friday + timedelta(days=2)
    local = datetime.combine(
        sunday, datetime.min.time(), tzinfo=ZoneInfo(config.MARKET_TZ)
    ).replace(hour=config.SUNDAY_OPEN_HOUR)
    return local.astimezone(timezone.utc)


def monday_noon_utc(weekend_start: str) -> datetime:
    friday = date.fromisoformat(weekend_start)
    monday = friday + timedelta(days=3)
    return datetime.combine(monday, datetime.min.time(), tzinfo=timezone.utc).replace(
        hour=config.MONDAY_COMPLETION_HOUR_UTC
    )


def price_at(fetch_range, target_utc: datetime, fallback=None):
    """The Brent price at a specific moment, not whenever a run happened to fire.

    Runs are dropped often enough that "the first run after X" can be hours
    after X, which would put the wrong number in a column named after a
    timestamp. Falls back to the caller's price only if the API has nothing.
    """
    from .outcomes import nearest_price

    if fetch_range is None:
        return fallback

    span = timedelta(minutes=config.OUTCOME_MATCH_WINDOW_MINUTES)
    points = fetch_range(target_utc - span, target_utc + span)
    found = nearest_price(points, target_utc) if points else None
    if found is None:
        print("no price near %s, falling back to this run's price" % target_utc.isoformat())
        return fallback
    return found


def on_open_run(client, now_chicago, now_utc, price, fetch_range=None) -> None:
    """Fill the reopen price, then the Monday price, as each becomes available."""
    from .sheets import WEEKEND_GAPS

    key = weekend_key(now_chicago)
    if key is None:
        return
    existing = client.find_row(WEEKEND_GAPS, "weekend_start_date", key)
    if existing is None:
        return

    if fetch_range is None and config.OUTCOME_USE_PRICE_API:
        from .price import fetch_brent_range as fetch_range

    if not str(existing.get("sunday_reopen_price", "")).strip():
        target = reopen_utc(key)
        if now_utc >= target:
            pinned = price_at(fetch_range, target, fallback=price)
            updates = reopen_updates(existing, pinned)
            if updates:
                client.update_cells(WEEKEND_GAPS, int(existing["_row"]), updates)
                existing.update(updates)

    if is_past_monday_noon_utc(now_utc) and not str(existing.get("monday_12utc_price", "")).strip():
        pinned = price_at(fetch_range, monday_noon_utc(key), fallback=price)
        updates = monday_updates(existing, pinned)
        if updates:
            client.update_cells(WEEKEND_GAPS, int(existing["_row"]), updates)
