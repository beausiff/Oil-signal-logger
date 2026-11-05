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
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
