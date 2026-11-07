"""What the locked rules WOULD have done, had the scorer never died.

    python -m scripts.replay

This is a simulation, twice over. The paper rules never ran on the 92 rows
that were rescored after the fact, and even the live rows only ever produced
paper positions. Nothing here is a trade that happened. The real record is the
`trades` tab, and it holds one closed trade.

It exists to answer one question the sheet cannot: over the whole period, with
every hour scored, would the rules have made money? It imports rules.py rather
than restating the logic, so what it replays is the locked rules themselves.

Three honest limits, in descending order of how much they matter:

  * Survivorship of the gap. 37% of rows were scored afterwards. The model was
    blind to what came next, but these were never acted on in real time.
  * Dropped runs. GitHub fired roughly one hour in four, so the replay sees
    the same sparse clock the live system did. A stop that would have hit
    between two runs is only caught at the next one.
  * One unit, no costs beyond the configured spread, no slippage, no funding.
"""

from __future__ import annotations

import sys
import random
from typing import List, Optional

from src import config, rules, sheets
from src.outcomes import parse_utc, to_float
from src.rules import FLAT, Position
from src.sheets import SIGNALS


def truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def replay(rows: List[dict]):
    position = FLAT
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

        if decision.action in ("buy", "sell"):
            position = Position(
                side=position.side, entry_price=position.entry_price,
                entry_utc=str(row.get("run_utc", "")), entry_score=position.entry_score,
                best_price=position.best_price,
                trade_id="R-%s-%s" % (str(row.get("run_utc", ""))[:16], position.side[0].upper()),
            )
        elif decision.action == "close" and before.is_open:
            exit_price = price if price is not None else before.best_price
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
                "source": str(row.get("score_source", "")) or "live",
            })

    return trades, position, skipped_no_price


def calendar_only(rows: List[dict]):
    """The same discipline with the news removed.

    The four replayed trades were all long and both winners were closed by the
    Friday rule, not by anything the score said. So this is the control: go
    long at the first open hour of each week and close on the Friday rule,
    reading no headlines at all. Same spread, same weekend flat stance.

    If this matches the scored strategy, the news is contributing nothing and
    what looked like a signal was a calendar.
    """
    position = FLAT
    trades = []
    seen_weeks = set()

    for row in rows:
        when = parse_utc(row.get("run_chicago", ""))
        if when is None:
            continue
        local = when.astimezone(rules.CHICAGO)
        price = to_float(row.get("brent_price"))
        if price is None or not rules.is_market_open(local):
            continue

        if position.is_open:
            position = rules.update_best_price(position, price)
            if rules.is_friday_close_run(local):
                usd, pct = rules.pnl("long", position.entry_price or 0.0, price)
                trades.append({"entry_utc": position.entry_utc, "entry_price": position.entry_price,
                               "exit_utc": str(row.get("run_utc", "")), "exit_price": price,
                               "pnl_usd": usd, "pnl_pct": pct})
                position = FLAT
            continue

        week = local.isocalendar()[:2]
        if week in seen_weeks or not rules.entries_allowed(local):
            continue
        seen_weeks.add(week)
        position = Position(side="long", entry_price=price, entry_utc=str(row.get("run_utc", "")),
                            entry_score=0, best_price=price, trade_id="CAL")

    return trades


def shuffled_strategy(rows: List[dict], rounds: int = 1000, seed: int = 20261107):
    """Replay the rules on shuffled scores, over and over.

    This keeps every price exactly where it is and only moves the scores
    around, which destroys any real link between a score and what followed it
    while leaving both distributions untouched. If the real strategy's result
    sits in the middle of this pile, the scores told it nothing.
    """
    pairs_in = [(r.get("score", ""), r.get("confidence", "")) for r in rows]
    rng = random.Random(seed)
    totals = []
    scratch = [dict(r) for r in rows]

    for _ in range(rounds):
        rng.shuffle(pairs_in)
        for row, (score, confidence) in zip(scratch, pairs_in):
            row["score"], row["confidence"] = score, confidence
        trades, _, _ = replay(scratch)
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

    print("\n  CONTROL 1: the same rules with the news removed")
    cal = calendar_only(rows)
    if not cal:
        print("    no trades")
    else:
        cal_usd = sum(t["pnl_usd"] for t in cal)
        cal_pct = sum(t["pnl_pct"] for t in cal)
        wins = sum(1 for t in cal if t["pnl_usd"] > 0)
        print("    long each week, flat by Friday, zero headlines read")
        print("    trades: %d   winners: %d (%.0f%%)   total: $%+.2f/bbl   sum of returns: %+.2f%%"
              % (len(cal), wins, 100.0 * wins / len(cal), cal_usd, cal_pct))
        print("    the scored strategy made $%+.2f/bbl, so the news is worth $%+.2f/bbl"
              % (real_total, real_total - cal_usd))

    print("\n  CONTROL 2: the same rules on shuffled scores, 1000 times")
    totals = shuffled_strategy(rows)
    beaten = sum(1 for t in totals if t >= real_total)
    p = (beaten + 1) / (len(totals) + 1)
    totals_sorted = sorted(totals)
    print("    shuffled median: $%+.2f/bbl   5th to 95th: $%+.2f to $%+.2f"
          % (totals_sorted[len(totals) // 2],
             totals_sorted[int(0.05 * len(totals))],
             totals_sorted[int(0.95 * len(totals))]))
    print("    real result $%+.2f/bbl beaten by %d of %d shuffles   p=%.3f   %s"
          % (real_total, beaten, len(totals), p,
             "SIGNAL" if p < 0.05 else "indistinguishable from chance"))

    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
