"""Locked trading rules and paper position bookkeeping.

Pure functions only: no network, no sheet access. Everything here is unit
tested, because these rules are the experiment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import config

CHICAGO = ZoneInfo(config.MARKET_TZ)

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)


def to_chicago(dt_utc: datetime) -> datetime:
    return dt_utc.astimezone(CHICAGO)


def is_market_open(now_chicago: datetime) -> bool:
    """CME crude hours: Sunday 17:00 open, Friday 16:00 close, 16:00-17:00 break."""
    day = now_chicago.weekday()
    hour = now_chicago.hour

    if day == SATURDAY:
        return False
    if day == SUNDAY:
        return hour >= config.SUNDAY_OPEN_HOUR
    if day == FRIDAY:
        return hour < config.FRIDAY_CLOSE_HOUR
    # Monday to Thursday: open except the daily maintenance break.
    return not (config.DAILY_BREAK_START_HOUR <= hour < config.DAILY_BREAK_END_HOUR)


def entries_allowed(now_chicago: datetime) -> bool:
    """No new entries after 10:00 Chicago on Friday."""
    if now_chicago.weekday() == FRIDAY and now_chicago.hour >= config.FRIDAY_NO_NEW_ENTRY_HOUR:
        return False
    return True


def is_friday_close_run(now_chicago: datetime) -> bool:
    """True from the last scheduled run before the Friday 16:00 close."""
    return (
        now_chicago.weekday() == FRIDAY
        and now_chicago.hour >= config.FRIDAY_FORCED_CLOSE_HOUR
        and now_chicago.hour < config.FRIDAY_CLOSE_HOUR
    )


def is_weekend_shutdown(now_chicago: datetime) -> bool:
    """Inside the Friday close to Sunday reopen shutdown."""
    day = now_chicago.weekday()
    if day == SATURDAY:
        return True
    if day == FRIDAY:
        return now_chicago.hour >= config.FRIDAY_CLOSE_HOUR
    if day == SUNDAY:
        return now_chicago.hour < config.SUNDAY_OPEN_HOUR
    return False


@dataclass(frozen=True)
class Position:
    side: str  # "long" | "short" | "flat"
    entry_price: Optional[float] = None
    entry_utc: Optional[str] = None
    entry_score: Optional[int] = None
    best_price: Optional[float] = None
    trade_id: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.side in ("long", "short")


FLAT = Position(side="flat")


@dataclass(frozen=True)
class Decision:
    action: str  # buy | sell | hold | close | flat | error
    position_after: Position
    exit_reason: Optional[str] = None
    notes: str = ""


def update_best_price(position: Position, price: float) -> Position:
    """Ratchet the best price seen since entry. Never moves against the trade."""
    if not position.is_open or price is None:
        return position
    if position.best_price is None:
        return replace(position, best_price=price)
    if position.side == "long":
        return replace(position, best_price=max(position.best_price, price))
    return replace(position, best_price=min(position.best_price, price))


def trailing_stop_hit(position: Position, price: float) -> bool:
    """3% retrace from the best price since entry, checked hourly."""
    if not position.is_open or price is None or position.best_price is None:
        return False
    band = config.TRAILING_STOP_PCT / 100.0
    if position.side == "long":
        return price <= position.best_price * (1 - band)
    return price >= position.best_price * (1 + band)


def reversal_exit(position: Position, score: int) -> bool:
    if not position.is_open or score is None:
        return False
    if position.side == "long":
        return score <= config.EXIT_LONG_SCORE
    return score >= config.EXIT_SHORT_SCORE


def pnl(side: str, entry_price: float, exit_price: float) -> tuple:
    """Dollars per barrel and percent, after one round trip of spread."""
    gross = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
    net = gross - config.SPREAD_USD_PER_BBL
    pct = (net / entry_price) * 100.0 if entry_price else 0.0
    return round(net, 4), round(pct, 4)


def decide(
    *,
    score: Optional[int],
    confidence: Optional[str],
    position: Position,
    now_chicago: datetime,
    price: Optional[float],
) -> Decision:
    """Apply the locked rules in priority order and return the resulting action."""
    market_open = is_market_open(now_chicago)

    if not market_open:
        if position.is_open and is_weekend_shutdown(now_chicago):
            # The Friday rule is "flat over the weekend". If the run that was
            # meant to close it never fired, close at the first opportunity
            # rather than carry the position through a weekend the test was
            # explicitly designed to sit out.
            return Decision(
                action="close",
                position_after=FLAT,
                exit_reason="friday",
                notes="friday_close_missed_closing_late",
            )
        note = "market_closed"
        if position.is_open:
            note = "market_closed_position_carried"
        return Decision(action="flat", position_after=position, notes=note)

    position = update_best_price(position, price)

    # 1. Friday forced close beats everything else.
    if position.is_open and is_friday_close_run(now_chicago):
        return Decision(
            action="close",
            position_after=FLAT,
            exit_reason="friday",
            notes="friday_forced_close",
        )

    # 2. Trailing stop, checked on price.
    if position.is_open and trailing_stop_hit(position, price):
        return Decision(
            action="close",
            position_after=FLAT,
            exit_reason="stop",
            notes="trailing_stop_%.1fpct" % config.TRAILING_STOP_PCT,
        )

    # 3. Reversal exit, checked on score.
    if position.is_open and reversal_exit(position, score):
        return Decision(
            action="close",
            position_after=FLAT,
            exit_reason="reversal",
            notes="reversal_score_%s" % score,
        )

    if position.is_open:
        return Decision(action="hold", position_after=position, notes="position_held")

    # 4. Entries. One position at a time, so we only get here when flat.
    if score is None or confidence not in config.ENTRY_CONFIDENCES:
        return Decision(action="flat", position_after=FLAT, notes="no_entry_signal")

    if price is None:
        return Decision(action="flat", position_after=FLAT, notes="no_price_no_entry")

    wants_long = score >= config.ENTRY_LONG_SCORE
    wants_short = score <= config.ENTRY_SHORT_SCORE

    if not (wants_long or wants_short):
        return Decision(action="flat", position_after=FLAT, notes="below_entry_threshold")

    if not entries_allowed(now_chicago):
        return Decision(action="flat", position_after=FLAT, notes="friday_no_new_entries")

    side = "long" if wants_long else "short"
    opened = Position(
        side=side,
        entry_price=price,
        entry_score=score,
        best_price=price,
    )
    return Decision(
        action="buy" if side == "long" else "sell",
        position_after=opened,
        notes="entry_score_%s_conf_%s" % (score, confidence),
    )
