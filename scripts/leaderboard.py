"""Six bots, one leaderboard. Version 2 of the experiment.

    python -m scripts.leaderboard

Every bot reads the same logged scores and prices and differs only in its
rules. They are computed by REPLAY over the `signals` rows, so there is no new
live state: the sheet's own state and trades tabs remain bot 1. Only rows
logged under rules_version "v2" race, and every bot starts flat at the first
of them.

With six bots one will lead by luck. A winner has to repeat in a second period
before it means anything, which is why bots 1 to 4 and 6 also get a luck p: how
often the same bot, run on the same prices with the scores slid round the clock
(scripts.replay.shuffled_strategy), matches or beats its real total.

Writes backup/leaderboard.json and the `leaderboard` sheet tab. Never trades.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

from src import rules, sheets
from src.outcomes import to_float
from src.sheets import SIGNALS
from scripts import replay

OUT_DIR = "backup"
TAB = "leaderboard"
MIN_TRADES_FOR_LUCK = 10
MIN_ROWS_FOR_LUCK = 60
STARTING_BALANCE = 100.0   # pretend dollars each bot starts the race with, all of it staked on every trade

# Registered on 2026-11-22. Must not be edited during the race: changing a bot
# afterwards would mean it was chosen with the results in view.
BOTS = [
    {"key": "original", "label": "Bot 1: Original", "variant": {}},
    {"key": "hold_longer", "label": "Bot 2: Hold longer", "variant": {"use_reversal": False}},
    {"key": "lower_bar", "label": "Bot 3: Lower bar", "variant": {"entry": 4}},
    {"key": "lower_bar_hold", "label": "Bot 4: Lower bar + hold longer",
     "variant": {"entry": 4, "use_reversal": False}},
    {"key": "calendar", "label": "Bot 5: Calendar only", "calendar": True},   # reads no news
    {"key": "opposite", "label": "Bot 6: Opposite", "variant": {}, "negate": True},
]


def race_rows(rows: List[dict]) -> List[dict]:
    """Rows logged under v2, in time order."""
    out = [r for r in rows if str(r.get("rules_version", "")).strip().startswith("v2")]
    out.sort(key=lambda r: str(r.get("run_utc", "")))
    return out


def negated(rows: List[dict]) -> List[dict]:
    """Every score flipped, confidence unchanged: shorts where the original buys."""
    out = []
    for row in rows:
        score = to_float(row.get("score"))
        out.append(dict(row, score=str(-int(score))) if score is not None else dict(row))
    return out


def run_bot(bot: dict, rows: List[dict]):
    """(closed trades, open position) for one bot over the race rows."""
    if bot.get("calendar"):
        return replay.calendar_only(rows, use_stop=False, with_position=True)
    if bot.get("negate"):
        rows = negated(rows)
    trades, position, _ = replay.replay_variant(rows, **bot.get("variant", {}))
    return trades, position


def last_price(rows: List[dict]) -> Optional[float]:
    for row in reversed(rows):
        value = to_float(row.get("brent_price"))
        if value is not None:
            return value
    return None


def first_price(rows: List[dict]) -> Optional[float]:
    for row in rows:
        value = to_float(row.get("brent_price"))
        if value is not None:
            return value
    return None


def luck_p(bot: dict, race: List[dict], real_total: float) -> Optional[float]:
    totals = replay.shuffled_strategy(race, runner=lambda scratch: run_bot(bot, scratch))
    if not totals:
        return None
    beaten = sum(1 for t in totals if t >= real_total)
    return (beaten + 1) / (len(totals) + 1)


def compound(trades: List[dict], start: float = STARTING_BALANCE) -> float:
    """Balance after the closed trades, whole balance staked each time (pnl_pct includes the spread)."""
    balance = start
    for t in trades:
        balance *= 1.0 + t["pnl_pct"] / 100.0
    return balance


def standings(rows: List[dict], with_luck: bool = True) -> dict:
    """The table. with_luck=False skips the shifted null, which costs seconds."""
    race = race_rows(rows)
    if not race:
        return {"started": False, "bots": [], "benchmark": None,
                "race_rows": 0, "race_start": ""}

    now_price = last_price(race)
    bots = []
    for bot in BOTS:
        trades, position = run_bot(bot, race)
        total = sum(t["pnl_usd"] for t in trades)
        balance = compound(trades)
        balance_now = balance
        open_position = None
        if position.is_open:
            unrealised = None
            if now_price is not None and position.entry_price is not None:
                unrealised, unrealised_pct = rules.pnl(position.side, position.entry_price, now_price)
                balance_now = balance * (1.0 + unrealised_pct / 100.0)
            open_position = {"side": position.side, "entry_price": position.entry_price,
                             "entry_utc": position.entry_utc, "unrealised_usd": unrealised}

        p = None
        if bot.get("calendar"):
            note = "n/a, reads no news"
        elif len(trades) < MIN_TRADES_FOR_LUCK:
            note = "need %d trades" % MIN_TRADES_FOR_LUCK
        elif len(race) < MIN_ROWS_FOR_LUCK:
            note = "need %d rows" % MIN_ROWS_FOR_LUCK
        elif not with_luck:
            note = "see analysis run"
        else:
            p = luck_p(bot, race, total)
            note = "" if p is not None else "too few rows to shift"

        bots.append({
            "key": bot["key"], "label": bot["label"],
            "trades": len(trades),
            "winners": sum(1 for t in trades if t["pnl_usd"] > 0),
            "total_usd": round(total, 4),
            "total_pct": round(sum(t["pnl_pct"] for t in trades), 4),
            "balance": round(balance, 4), "balance_now": round(balance_now, 4),
            "worst_usd": min([t["pnl_usd"] for t in trades if t["pnl_usd"] < 0], default=0),
            "open": open_position,
            "luck_p": p, "luck_note": note,
        })

    first = first_price(race)
    benchmark = {"first_price": first, "last_price": now_price,
                 "usd": None, "pct": None,
                 "balance_now": STARTING_BALANCE if first and now_price is not None else None}
    if first and now_price is not None:
        benchmark["usd"] = round(now_price - first, 4)
        benchmark["pct"] = round((now_price - first) / first * 100.0, 4)
        benchmark["balance_now"] = round(STARTING_BALANCE * now_price / first, 4)

    return {"started": True, "bots": bots, "benchmark": benchmark,
            "race_rows": len(race), "race_start": str(race[0].get("run_utc", "")),
            "generated_utc": str(race[-1].get("run_utc", ""))}


def ranked(table: dict) -> List[dict]:
    """Best balance first; ties keep bot number order (sorted is stable)."""
    return sorted(table["bots"], key=lambda b: -b["balance_now"])


def _open_text(bot: dict) -> str:
    o = bot["open"]
    if not o:
        return "flat"
    text = "%s from %s" % (o["side"], o["entry_price"])
    if o["unrealised_usd"] is not None:
        text += " (%+.2f)" % o["unrealised_usd"]
    return text


def _luck_text(bot: dict) -> str:
    return "%.3f" % bot["luck_p"] if bot["luck_p"] is not None else bot["luck_note"]


def write_tab(client, table: dict, updated_utc: Optional[str] = None) -> None:
    """Overwrite the leaderboard tab: header, bots in rank order, blank row,
    benchmark row, updated_utc."""
    rows = [["rank", "bot", "$100 is now", "trades", "winners", "total_usd", "total_pct", "worst_usd",
             "open_side", "open_entry", "open_unrealised_usd", "luck_p", "luck_note"]]
    for rank, bot in enumerate(ranked(table), start=1):
        o = bot["open"] or {}
        rows.append([rank, bot["label"], bot["balance_now"], bot["trades"], bot["winners"], bot["total_usd"],
                     bot["total_pct"], bot["worst_usd"], o.get("side", "flat"),
                     o.get("entry_price", ""), o.get("unrealised_usd", ""),
                     bot["luck_p"] if bot["luck_p"] is not None else "", bot["luck_note"]])
    rows.append([])
    b = table["benchmark"] or {}
    rows.append(["benchmark", "Buy and hold", b.get("balance_now") or "", "", "", b.get("usd", ""), b.get("pct", ""), "",
                 "", b.get("first_price", ""), b.get("last_price", ""), "", "first and last price"])
    rows.append(["updated_utc", updated_utc or datetime.now(timezone.utc).isoformat()])
    client.write_table(TAB, rows)


def print_table(table: dict) -> None:
    print("race started %s, %d race rows" % (table["race_start"], table["race_rows"]))
    print("\n  %-4s %-32s %11s %6s %8s %12s %9s %10s  %-26s %s"
          % ("rank", "bot", "$100 is now", "trades", "winners", "total $/bbl", "total %", "worst $", "open", "luck p"))
    for rank, bot in enumerate(ranked(table), start=1):
        print("  %-4d %-32s %11.2f %6d %8d %12.2f %8.2f%% %10.2f  %-26s %s"
              % (rank, bot["label"], bot["balance_now"], bot["trades"], bot["winners"], bot["total_usd"],
                 bot["total_pct"], bot["worst_usd"], _open_text(bot), _luck_text(bot)))
    b = table["benchmark"]
    if b and b["usd"] is not None:
        print("\n  buy and hold over the same window: %.2f to %.2f = %+.2f/bbl (%+.2f%%), $100 is now %.2f"
              % (b["first_price"], b["last_price"], b["usd"], b["pct"], b["balance_now"]))
    print("\n  Six bots: one will lead by luck. A winner has to repeat in a second period.")


def main() -> int:
    client = sheets.open_client(dry_run=False)
    rows = client.records(SIGNALS)
    table = standings(rows)
    if not table["started"]:
        print("race has not started: no v2 rows yet")
        return 0
    print_table(table)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "leaderboard.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(table, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print("wrote %s" % path)

    try:
        write_tab(client, table)
    except Exception as exc:  # noqa: BLE001 - the printout and the file are the deliverable
        print("leaderboard tab not written: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
