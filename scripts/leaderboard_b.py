"""Feed B standings: the six bots on NewsAPI.ai headlines, and the pairs.

    python -m scripts.leaderboard_b

Reuses scripts.leaderboard (BOTS, standings, race_rows, STARTING_BALANCE)
unchanged and replays the `signals_b` rows. Labels are relabelled at output only:
"Bot 1: Original" becomes "Bot 1B: Original".

The paired comparison is the point of feed B. For each of the six rules it gives
the main bot's balance and the B bot's balance, both computed ONLY over the rows
whose run_utc exists in both feeds (the common period, each bot starting flat at
the first common row), so a difference is the news feed and not the clock.

Writes backup/leaderboard_b.json and the `leaderboard_b` sheet tab. Never trades.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

from scripts import leaderboard
from src.outcomes import parse_utc

OUT_DIR = "backup"
TAB = "leaderboard_b"
_LABEL = re.compile(r"^Bot (\d+):")


def relabel(label: str) -> str:
    """"Bot 1: Original" -> "Bot 1B: Original"."""
    return _LABEL.sub(lambda m: "Bot %sB:" % m.group(1), label)


def _key(row: dict) -> Optional[datetime]:
    return parse_utc(str(row.get("run_utc", "")))


def common_rows(main_rows: List[dict], b_rows: List[dict]):
    """(main race rows, B race rows) restricted to run_utc values present in both."""
    main_race = leaderboard.race_rows(main_rows)
    b_race = leaderboard.race_rows(b_rows)
    shared = {_key(r) for r in main_race if _key(r)} & {_key(r) for r in b_race if _key(r)}
    return ([r for r in main_race if _key(r) in shared],
            [r for r in b_race if _key(r) in shared])


def paired(main_rows: List[dict], b_rows: List[dict]) -> dict:
    main_common, b_common = common_rows(main_rows, b_rows)
    out = {"common_rows": len(b_common),
           "common_start": str(b_common[0]["run_utc"]) if b_common else "",
           "common_end": str(b_common[-1]["run_utc"]) if b_common else "",
           "pairs": []}
    if not b_common:
        return out
    main_bots = {b["key"]: b for b in leaderboard.standings(main_common, with_luck=False)["bots"]}
    b_bots = {b["key"]: b for b in leaderboard.standings(b_common, with_luck=False)["bots"]}
    for number, bot in enumerate(leaderboard.BOTS, 1):
        m, b = main_bots[bot["key"]], b_bots[bot["key"]]
        out["pairs"].append({
            "key": bot["key"], "number": number,
            "main_label": bot["label"], "b_label": relabel(bot["label"]),
            "main_balance": m["balance_now"], "b_balance": b["balance_now"],
            "diff": round(b["balance_now"] - m["balance_now"], 4),
            "main_trades": m["trades"], "b_trades": b["trades"],
        })
    return out


def standings_b(b_rows: List[dict]) -> dict:
    """The leaderboard table over the B rows, labels relabelled to "Bot NB"."""
    table = leaderboard.standings(b_rows, with_luck=False)
    for bot in table["bots"]:
        bot["label"] = relabel(bot["label"])
    return table


def build(main_rows: List[dict], b_rows: List[dict], state_start: str = "") -> dict:
    table = standings_b(b_rows)
    table["paired"] = paired(main_rows, b_rows)
    table["feed_b_start"] = state_start or (table["race_start"] if table["started"] else "")
    return table


def tab_rows(table: dict, updated_utc: Optional[str] = None) -> List[list]:
    rows = [["rank", "bot", "$100 is now", "trades", "winners", "total_usd", "total_pct", "worst_usd",
             "open_side", "open_entry", "open_unrealised_usd"]]
    if table["started"]:
        for rank, bot in enumerate(leaderboard.ranked(table), start=1):
            o = bot["open"] or {}
            rows.append([rank, bot["label"], bot["balance_now"], bot["trades"], bot["winners"],
                         bot["total_usd"], bot["total_pct"], bot["worst_usd"], o.get("side", "flat"),
                         o.get("entry_price", ""), o.get("unrealised_usd", "")])
        b = table["benchmark"] or {}
        rows.append([])
        rows.append(["benchmark", "Buy and hold", b.get("balance_now") or "", "", "", b.get("usd", ""),
                     b.get("pct", "")])
    else:
        rows.append(["", "feed B has not started"])
    p = table["paired"]
    rows.append([])
    rows.append(["paired comparison over the common period (rows in both feeds)"])
    rows.append(["pair", "main bot", "B bot", "main $100 is now", "B $100 is now", "B minus main",
                 "main trades", "B trades"])
    for pair in p["pairs"]:
        rows.append(["%d vs %dB" % (pair["number"], pair["number"]), pair["main_label"], pair["b_label"],
                     pair["main_balance"], pair["b_balance"], pair["diff"],
                     pair["main_trades"], pair["b_trades"]])
    rows.append(["common_rows", p["common_rows"], "from", p["common_start"], "to", p["common_end"]])
    rows.append(["feed_b_start", table.get("feed_b_start", "")])
    rows.append(["updated_utc", updated_utc or datetime.now(timezone.utc).isoformat()])
    return rows


def write_tab(store, table: dict, updated_utc: Optional[str] = None) -> None:
    store.write_table(TAB, tab_rows(table, updated_utc))


def write_json(table: dict, out_dir: str = OUT_DIR) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "leaderboard_b.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(table, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return path


def print_table(table: dict) -> None:
    if not table["started"]:
        print("feed B has not started: no signals_b rows yet")
        return
    print("feed B started %s, %d race rows" % (table["feed_b_start"], table["race_rows"]))
    for rank, bot in enumerate(leaderboard.ranked(table), start=1):
        print("  %d %-34s $100 is now %8.2f  trades %d" % (rank, bot["label"], bot["balance_now"], bot["trades"]))
    p = table["paired"]
    print("paired over %d common rows:" % p["common_rows"])
    for pair in p["pairs"]:
        print("  %d vs %dB  main %8.2f  B %8.2f  diff %+.2f" % (
            pair["number"], pair["number"], pair["main_balance"], pair["b_balance"], pair["diff"]))


def main() -> int:
    from src import feed_b, news_b
    try:
        store = feed_b.SheetStore()
        main_rows = store.main_rows()
        b_rows = store.rows(feed_b.SIGNALS_B)
        table = build(main_rows, b_rows, state_start=store.get_state().get("start_utc", ""))
        print_table(table)
        print("wrote %s" % write_json(table))
        try:
            write_tab(store, table)
        except Exception as exc:  # noqa: BLE001 - the printout and the file are the deliverable
            news_b.say("leaderboard_b tab not written: %s" % exc)
    except Exception as exc:  # noqa: BLE001 - never fail a workflow over a side table
        news_b.say("leaderboard_b skipped: %s: %s" % (type(exc).__name__, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
