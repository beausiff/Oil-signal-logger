"""What the locked rules WOULD have done, had the scorer never died.

    python -m scripts.replay

This is a simulation, twice over. The paper rules never ran on the rows that
were rescored after the fact, and even the live rows only ever produced paper
positions. Nothing here is a trade that happened. The real record is the
`trades` tab.

It exists to answer one question the sheet cannot: over the whole period, with
every hour scored, would the rules have made money? It imports rules.py rather
than restating the logic, so what it replays is the locked rules themselves.

Three honest limits, in descending order of how much they matter:

  * Survivorship of the gap. A large share of rows were scored afterwards
    (score_source starts with "backfilled"). The model was blind to what came
    next, but these were never acted on in real time.
  * Dropped runs. GitHub fires only a few scheduled runs a day, so the replay
    sees the same sparse clock the live system did. A stop that would have hit
    between two runs is only caught at the next one.
  * One unit, no costs beyond the configured spread, no slippage, no funding.

The two controls below are there to try to kill the result, and with this few
trades neither proves anything either way.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from typing import List, Optional

from src import config, rules, sheets
from src.outcomes import parse_utc, to_float
from src.rules import FLAT, Position
from src.sheets import SIGNALS


def truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def replay(rows: List[dict]):
    return _replay_loop(rows)


def replay_variant(rows: List[dict], entry: Optional[int] = None, use_reversal: bool = True):
    """The replay with one of two switches changed. Live rules are untouched.

    entry=N overrides the entry threshold to +/-N for this call only, in
    memory, and restores config in a finally. use_reversal=False ignores a
    reversal exit and leaves the position open as a hold would; the Friday
    close, the late weekend close and the trailing stop still apply.
    """
    if entry is None:
        return _replay_loop(rows, use_reversal=use_reversal)
    saved = (config.ENTRY_LONG_SCORE, config.ENTRY_SHORT_SCORE)
    config.ENTRY_LONG_SCORE, config.ENTRY_SHORT_SCORE = entry, -entry
    try:
        return _replay_loop(rows, use_reversal=use_reversal)
    finally:
        config.ENTRY_LONG_SCORE, config.ENTRY_SHORT_SCORE = saved


def _replay_loop(rows: List[dict], use_reversal: bool = True):
    position = FLAT
    entry_source = ""
    last_price = None
    trades = []
    skipped_no_price = 0

    for row in rows:
        when = parse_utc(row.get("run_chicago", ""))
        if when is None:
            continue
        local = when.astimezone(rules.CHICAGO)

        raw_score = to_float(row.get("score"))
        score = int(raw_score) if raw_score is not None else None
        confidence = (row.get("confidence") or "").strip() or None
        price = to_float(row.get("brent_price"))
        if price is None:
            skipped_no_price += 1

        before = position
        decision = rules.decide(
            score=score, confidence=confidence, position=position,
            now_chicago=local, price=price,
        )
        position = decision.position_after
        if not use_reversal and decision.action == "close" and decision.exit_reason == "reversal":
            # Ignored: stay open exactly as a hold would.
            decision = rules.Decision(action="hold", position_after=before)
            position = before
            if price is not None and rules.is_market_open(local):
                position = rules.update_best_price(before, price)

        if decision.action in ("buy", "sell"):
            position = Position(
                side=position.side, entry_price=position.entry_price,
                entry_utc=str(row.get("run_utc", "")), entry_score=position.entry_score,
                best_price=position.best_price,
                trade_id="R-%s-%s" % (str(row.get("run_utc", ""))[:16], position.side[0].upper()),
            )
            entry_source = str(row.get("score_source", "")) or "live"
        elif decision.action == "close" and before.is_open:
            # No quote: the last price seen, never the best price, which would
            # book the exit at the most favourable print of the trade.
            exit_price = price
            if exit_price is None:
                exit_price = last_price if last_price is not None else before.entry_price
            usd, pct = rules.pnl(before.side, before.entry_price or 0.0, exit_price or 0.0)
            entry_at = parse_utc(before.entry_utc or "")
            exit_at = parse_utc(str(row.get("run_utc", "")))
            hours = round((exit_at - entry_at).total_seconds() / 3600.0, 1) if (entry_at and exit_at) else ""
            trades.append({
                "side": before.side,
                "entry_utc": before.entry_utc, "entry_price": before.entry_price,
                "entry_score": before.entry_score,
                "exit_utc": str(row.get("run_utc", "")), "exit_price": exit_price,
                "reason": decision.exit_reason or "manual",
                "pnl_usd": usd, "pnl_pct": pct, "hours": hours,
                "source": entry_source,
            })

        if price is not None:
            last_price = price

    return trades, position, skipped_no_price


def _trading_week(local):
    """(iso year, iso week) of the Monday to Friday week this row trades in.

    Sunday evening belongs to the Monday that follows it, so a run just after
    the Sunday reopen and the first run on Monday count as the same week.
    """
    day = local.date()
    if local.weekday() == rules.SUNDAY:
        day += timedelta(days=1)
    return day.isocalendar()[:2]


def calendar_only(rows: List[dict], use_stop: bool = False, with_position: bool = False):
    """The same calendar discipline with the news removed.

    Go long at the first priced, open, entry-allowed Monday to Friday row of each
    trading week,
    read no headlines at all, and leave on the same exits the rules use that
    need no score: the Friday close run, the first row inside the weekend
    shutdown if no run landed in the Friday close hour (closed at that row's
    price, as rules.decide does), and optionally the trailing stop. One entry
    per week, never held across a weekend. The reversal exit is not applied,
    because it needs a score.

    It is a control, not a verdict. If it matches the scored strategy, the
    news is contributing nothing and what looked like a signal was a calendar.
    With this few trades neither outcome is conclusive. The result is also
    sensitive to entry timing (allowing Sunday evening entries gives -$3.21 with
    no stop and -$21.92 with the stop on the 310 row backup), so it is a rough
    control.

    with_position=True returns (trades, open_position) instead of just trades.
    """
    position = FLAT
    trades = []
    seen_weeks = set()
    last_price = None

    def close(row, price, reason):
        usd, pct = rules.pnl("long", position.entry_price or 0.0, price)
        trades.append({"side": "long", "entry_utc": position.entry_utc,
                       "entry_price": position.entry_price,
                       "exit_utc": str(row.get("run_utc", "")), "exit_price": price,
                       "reason": reason, "pnl_usd": usd, "pnl_pct": pct})

    for row in rows:
        when = parse_utc(row.get("run_chicago", ""))
        if when is None:
            continue
        local = when.astimezone(rules.CHICAGO)
        price = to_float(row.get("brent_price"))
        is_open = rules.is_market_open(local)

        if position.is_open:
            if is_open and price is not None:
                position = rules.update_best_price(position, price)
            exit_price = price if price is not None else last_price
            reason = None
            if rules.is_weekend_shutdown(local):
                reason = "friday"
            elif is_open and rules.is_friday_close_run(local):
                reason = "friday"
            elif use_stop and is_open and rules.trailing_stop_hit(position, price):
                reason = "stop"
            if reason and exit_price is not None:
                close(row, exit_price, reason)
                position = FLAT
        elif is_open and price is not None and local.weekday() != rules.SUNDAY:
            # Not on the thin Sunday reopen: the week's entry is its first
            # Monday to Friday row, and Sunday evening is the same week.
            week = _trading_week(local)
            if week not in seen_weeks and rules.entries_allowed(local):
                seen_weeks.add(week)
                position = Position(side="long", entry_price=price,
                                    entry_utc=str(row.get("run_utc", "")),
                                    entry_score=0, best_price=price, trade_id="CAL")

        if price is not None:
            last_price = price

    return (trades, position) if with_position else trades


def shuffled_strategy(rows: List[dict], min_offset: int = 10, runner=None):
    """Replay the rules on circularly shifted scores, at every offset.

    This keeps every price and timestamp exactly where it is and slides the
    (score, confidence) sequence round the clock by k rows, for every k from
    min_offset to n - min_offset. That breaks the link between a score and
    what followed it while keeping the scores' own persistence (consecutive
    hours share news, so they repeat), which a plain shuffle destroys and which
    makes a plain shuffle's null too easy to beat. Returns one total per
    offset. If the real result sits in the middle of this pile, the scores told
    it nothing. runner(rows) -> (trades, ...) defaults to the locked replay.
    """
    runner = runner or replay
    n = len(rows)
    if n < 3 * min_offset:
        return []
    pairs_in = [(r.get("score", ""), r.get("confidence", "")) for r in rows]
    totals = []
    scratch = [dict(r) for r in rows]

    for k in range(min_offset, n - min_offset + 1):
        for i, row in enumerate(scratch):
            row["score"], row["confidence"] = pairs_in[(i + k) % n]
        trades = runner(scratch)[0]
        totals.append(sum(t["pnl_usd"] for t in trades))
    return totals


def main() -> int:
    client = sheets.open_client(dry_run=False)
    rows = client.records(SIGNALS)
    rows.sort(key=lambda r: str(r.get("run_utc", "")))

    print("=" * 78)
    print("REPLAY: what the locked rules would have done. NOT a trade record.")
    print("=" * 78)
    print("rules version: %s   entry +/-%d   stop %.1f%%   spread $%.2f/bbl"
          % (config.RULES_VERSION, config.ENTRY_LONG_SCORE,
             config.TRAILING_STOP_PCT, config.SPREAD_USD_PER_BBL))
    print("signal rows replayed: %d" % len(rows))

    trades, open_position, no_price = replay(rows)
    if no_price:
        print("rows with no price: %d" % no_price)

    if not trades:
        print("\nno completed trades")
    else:
        print("\n  %-5s %-16s %-8s %-16s %-8s %-9s %8s %7s  %s"
              % ("side", "entry", "price", "exit", "price", "reason", "pnl$", "pnl%", "hrs"))
        for t in trades:
            print("  %-5s %-16s %-8s %-16s %-8s %-9s %8.2f %6.2f%% %6s"
                  % (t["side"], str(t["entry_utc"])[:16], t["entry_price"],
                     str(t["exit_utc"])[:16], t["exit_price"], t["reason"],
                     t["pnl_usd"], t["pnl_pct"], t["hours"]))

        wins = [t for t in trades if t["pnl_usd"] > 0]
        total_usd = sum(t["pnl_usd"] for t in trades)
        total_pct = sum(t["pnl_pct"] for t in trades)
        print("\n  trades: %d   winners: %d (%.0f%%)   total: $%.2f/bbl   sum of returns: %+.2f%%"
              % (len(trades), len(wins), 100.0 * len(wins) / len(trades), total_usd, total_pct))

        by_reason = {}
        for t in trades:
            by_reason.setdefault(t["reason"], []).append(t["pnl_usd"])
        print("  exits:", "  ".join(
            "%s %d ($%+.2f)" % (k, len(v), sum(v)) for k, v in sorted(by_reason.items())))

        longs = [t for t in trades if t["side"] == "long"]
        shorts = [t for t in trades if t["side"] == "short"]
        for name, group in (("long", longs), ("short", shorts)):
            if group:
                print("  %-5s %d trades, $%+.2f/bbl" % (name, len(group), sum(t["pnl_usd"] for t in group)))

    if open_position.is_open:
        print("\n  still open at the end: %s from %s" % (open_position.side, open_position.entry_price))

    # The benchmark that matters: doing nothing at all.
    priced = [to_float(r.get("brent_price")) for r in rows]
    priced = [p for p in priced if p is not None]
    if len(priced) >= 2:
        first, last = priced[0], priced[-1]
        print("\n  buy and hold over the same window: %.2f to %.2f = %+.2f%%"
              % (first, last, (last - first) / first * 100.0))
    real_total = sum(t["pnl_usd"] for t in trades)

    print("\n  CONTROL 1: the calendar alone, no headlines read")
    print("    long at the first open weekday row of each week, out by the Friday close.")
    print("    A control, not a verdict: n is small, so a gap either way proves little.")
    for label, use_stop in (("no stop  ", False), ("3% stop   ", True)):
        cal = calendar_only(rows, use_stop=use_stop)
        if not cal:
            print("    %s no trades" % label)
            continue
        wins = sum(1 for t in cal if t["pnl_usd"] > 0)
        print("    %s trades: %d   winners: %d (%.0f%%)   total: $%+.2f/bbl   sum of returns: %+.2f%%"
              % (label, len(cal), wins, 100.0 * wins / len(cal),
                 sum(t["pnl_usd"] for t in cal), sum(t["pnl_pct"] for t in cal)))
    print("    the scored strategy made $%+.2f/bbl over %d trades" % (real_total, len(trades)))

    totals = shuffled_strategy(rows)
    print("\n  CONTROL 2: the same rules on circularly shifted scores, %d offsets" % len(totals))
    if not totals:
        print("    too few rows to shift")
    else:
        beaten = sum(1 for t in totals if t >= real_total)
        p = (beaten + 1) / (len(totals) + 1)
        totals_sorted = sorted(totals)
        print("    shifted median: $%+.2f/bbl   5th to 95th: $%+.2f to $%+.2f"
              % (totals_sorted[len(totals) // 2],
                 totals_sorted[int(0.05 * len(totals))],
                 totals_sorted[int(0.95 * len(totals))]))
        print("    real result $%+.2f/bbl matched or beaten by %d of %d shifts   p=%.3f   %s"
              % (real_total, beaten, len(totals), p,
                 "SIGNAL" if p < 0.05 else "indistinguishable from chance"))


    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
