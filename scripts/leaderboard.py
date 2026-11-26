"""Sixteen bots, two cohorts, one leaderboard. Version 2 of the experiment.

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

Bots 7 to 16 were registered on 2026-11-25, after bots 1 to 6 had been racing
(bots 15 and 16 before the cohort 2 race started), and need the context columns (context_version c1, vol20_pct, ma200,
event_flags). Judging them on rows logged before they existed would be
cheating, so they form cohort 2: they race only over v2 rows from the first
row that has a context_version, each starting flat there. Cohort 1 (bots 1 to
6) is untouched. Cohort 2 also gets a reference entry, Bot 1's rules over the
same rows, so every new bot can be compared like for like. With 16 bots Bot 1
will lead some of the time by luck alone.

Writes backup/leaderboard.json and the `leaderboard` sheet tab, and every
closed trade and open position of every bot to backup/bot_trades.json and the
`bot_trades` tab. Never trades.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src import config, rules, sheets
from src.outcomes import parse_utc, to_float
from src.sheets import SIGNALS
from scripts import replay

OUT_DIR = "backup"
TAB = "leaderboard"
BOT_TRADES_TAB = "bot_trades"
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

# Bots 7 to 16 were registered on 2026-11-25, after bots 1 to 6 were racing. Must not be edited
# during the race, for the same reason: changing a bot afterwards would mean it
# was chosen with the results in view. Each differs from Bot 1 by one change
# unless stated. They are a separate list, not appended to BOTS, because
# scripts/build_page.py walks BOTS and needs rule text and a card for every
# entry; ALL_BOTS is the full registered order, 1 to 16.
NEW_BOTS = [
    {"key": "wide_stop", "label": "Bot 7: Wide stop", "cohort": 2,
     "variant": {"stop_pct": 8.0}},
    {"key": "vol_stop", "label": "Bot 8: Volatility stop", "cohort": 2,
     "variant": {"vol_stop_mult": 3.0}},
    {"key": "short_hold", "label": "Bot 9: Short hold", "cohort": 2,
     "variant": {"max_hold_hours": 24}},
    {"key": "fade_24h", "label": "Bot 10: Fade 24 hours", "cohort": 2,
     "variant": {"max_hold_hours": 24}, "negate": True},    # Bot 6 plus the time exit
    {"key": "trend_filter", "label": "Bot 11: Trend filter", "cohort": 2,
     "variant": {"trend_filter": True}},
    {"key": "event_standaside", "label": "Bot 12: Event stand aside", "cohort": 2,
     "variant": {"event_standaside": True}},
    {"key": "vol_size", "label": "Bot 13: Volatility size", "cohort": 2,
     "variant": {"vol_size": True}},
    {"key": "safeguards", "label": "Bot 14: Safeguards", "cohort": 2,
     "variant": {"vol_stop_mult": 3.0, "trend_filter": True,
                 "event_standaside": True, "vol_size": True}},
    # Bots 15 and 16 were added on 2026-11-25 as well, before the cohort 2 race
    # started (no row had a context_version yet), so they race from its first row.
    {"key": "trend_bar", "label": "Bot 15: Trend lowers the bar", "cohort": 2,
     "variant": {"trend_entry": 4}},
    {"key": "trend_only", "label": "Bot 16: Trend only", "cohort": 2,
     "trend_only": True},    # reads no news
]
ALL_BOTS = BOTS + NEW_BOTS

# Not a bot: Bot 1's rules over the cohort 2 rows, so every new bot has a like
# for like comparison.
REFERENCE_BOT = {"key": "original_same_window", "label": "Bot 1 over the same window",
                 "variant": {}, "cohort": 2, "reference": True}


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


def cohort2_rows(rows: List[dict]) -> List[dict]:
    """The race rows from the first one that has a context_version onward.

    Empty until the logger has written one. Rows after the first are kept even
    if one of them lacks the marker: the cohort starts once and stays started.
    """
    race = race_rows(rows)
    for i, row in enumerate(race):
        if str(row.get("context_version") or "").strip():
            return race[i:]
    return []


def run_bot_full(bot: dict, rows: List[dict]):
    """(closed trades, open position, info) for one bot. info carries the
    missing input counter and the open trade's size and stop width."""
    if bot.get("calendar"):
        trades, position = replay.calendar_only(rows, use_stop=False, with_position=True)
        return trades, position, {}
    if bot.get("trend_only"):
        info = {}
        trades, position = replay.trend_only(rows, with_position=True, info=info)
        return trades, position, info
    if bot.get("negate"):
        rows = negated(rows)
    info = {}
    trades, position, _ = replay.replay_variant(rows, info=info, **bot.get("variant", {}))
    return trades, position, info


def run_bot(bot: dict, rows: List[dict]):
    """(closed trades, open position) for one bot over the race rows."""
    trades, position, _ = run_bot_full(bot, rows)
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


def sized_usd(trade: dict) -> float:
    """The trade's dollars at its size. Trades with no size are size 1, untouched."""
    return trade["pnl_usd"] * trade["size"] if "size" in trade else trade["pnl_usd"]


def sized_pct(trade: dict) -> float:
    return trade["pnl_pct"] * trade["size"] if "size" in trade else trade["pnl_pct"]


def _sized_trades(trades: List[dict]) -> List[dict]:
    return [dict(t, pnl_usd=sized_usd(t)) if "size" in t else t for t in trades]


def luck_p(bot: dict, race: List[dict], real_total: float) -> Optional[float]:
    """Share of circular score shifts that match or beat the real total.

    Deterministic: the shifts are every offset, there is no random draw. The
    shuffle moves only score and confidence, so ma200, vol20_pct and
    event_flags stay with the prices they were logged beside. A negated bot is
    negated after the shift, as it is in the real run, and a sized bot's
    shifted totals are sized the same way as its real total.
    """
    totals = replay.shuffled_strategy(
        race, runner=lambda scratch: (_sized_trades(run_bot(bot, scratch)[0]),))
    if not totals:
        return None
    beaten = sum(1 for t in totals if t >= real_total)
    return (beaten + 1) / (len(totals) + 1)


def compound(trades: List[dict], start: float = STARTING_BALANCE) -> float:
    """Balance after the closed trades, whole balance staked each time (pnl_pct includes the spread).

    A trade with a size stakes size times the balance, so its return is
    pnl_pct * size. Trades without a size are size 1.
    """
    balance = start
    for t in trades:
        balance *= 1.0 + sized_pct(t) / 100.0
    return balance


def _luck(bot: dict, race: List[dict], trades: List[dict], total: float, with_luck: bool):
    if bot.get("calendar") or bot.get("trend_only"):
        return None, "n/a, reads no news"
    if len(trades) < MIN_TRADES_FOR_LUCK:
        return None, "need %d trades" % MIN_TRADES_FOR_LUCK
    if len(race) < MIN_ROWS_FOR_LUCK:
        return None, "need %d rows" % MIN_ROWS_FOR_LUCK
    if not with_luck:
        return None, "see analysis run"
    p = luck_p(bot, race, total)
    return p, ("" if p is not None else "too few rows to shift")


def _bot_entry(bot: dict, race: List[dict], now_price: Optional[float], with_luck: bool) -> dict:
    trades, position, info = run_bot_full(bot, race)
    total = sum(sized_usd(t) for t in trades)
    balance = compound(trades)
    balance_now = balance
    open_position = None
    if position.is_open:
        unrealised = None
        size = (info.get("open") or {}).get("size")
        if now_price is not None and position.entry_price is not None:
            unrealised, unrealised_pct = rules.trade_pnl(
                position.side, position.entry_price, now_price, parse_utc(position.entry_utc or ""),
                parse_utc(str(race[-1].get("run_utc", ""))) if race else None)
            if size is not None:
                unrealised, unrealised_pct = unrealised * size, unrealised_pct * size
            balance_now = balance * (1.0 + unrealised_pct / 100.0)
        open_position = {"side": position.side, "entry_price": position.entry_price,
                         "entry_utc": position.entry_utc, "unrealised_usd": unrealised}
        if size is not None:
            open_position["size"] = size

    p, note = _luck(bot, race, trades, total, with_luck)
    usd = [sized_usd(t) for t in trades]
    entry = {
        "key": bot["key"], "label": bot["label"], "cohort": bot.get("cohort", 1),
        "trades": len(trades),
        "winners": sum(1 for t in trades if t["pnl_usd"] > 0),
        "total_usd": round(total, 4),
        "total_pct": round(sum(sized_pct(t) for t in trades), 4),
        "balance": round(balance, 4), "balance_now": round(balance_now, 4),
        "worst_usd": min([u for u in usd if u < 0], default=0),
        "open": open_position,
        "luck_p": p, "luck_note": note,
    }
    if entry["cohort"] == 2:
        entry.update({"state": "running",
                      "missing_input_decisions": info.get("missing_input_decisions", 0),
                      "missing_by_feature": info.get("missing_by_feature", {}),
                      "blocked_entries": info.get("blocked_entries", {})})
        if bot.get("reference"):
            entry["reference"] = True
    return entry


def _not_started_entry(bot: dict) -> dict:
    entry = {"key": bot["key"], "label": bot["label"], "cohort": 2, "trades": 0, "winners": 0,
             "total_usd": 0, "total_pct": 0, "balance": STARTING_BALANCE, "balance_now": STARTING_BALANCE,
             "worst_usd": 0, "open": None, "luck_p": None, "luck_note": "not started",
             "state": "not started", "missing_input_decisions": 0, "missing_by_feature": {},
             "blocked_entries": {}}
    if bot.get("reference"):
        entry["reference"] = True
    return entry


def _benchmark(race: List[dict], now_price: Optional[float]) -> dict:
    first = first_price(race)
    benchmark = {"first_price": first, "last_price": now_price,
                 "usd": None, "pct": None,
                 "balance_now": STARTING_BALANCE if first and now_price is not None else None}
    if first and now_price is not None:
        benchmark["usd"] = round(now_price - first, 4)
        benchmark["pct"] = round((now_price - first) / first * 100.0, 4)
        benchmark["balance_now"] = round(STARTING_BALANCE * now_price / first, 4)
    return benchmark


def _cohort2_section(rows: List[dict], with_luck: bool) -> dict:
    """Bots 7 to 16 plus the reference, over their own rows only."""
    race = cohort2_rows(rows)
    if not race:
        return {"started": False, "state": "not started",
                "note": "waiting for the first v2 row with a context_version",
                "rows": 0, "start_utc": "",
                "bots": [_not_started_entry(b) for b in NEW_BOTS],
                "reference": _not_started_entry(REFERENCE_BOT), "benchmark": None}
    now_price = last_price(race)
    return {"started": True, "state": "running", "note": "",
            "rows": len(race), "start_utc": str(race[0].get("run_utc", "")),
            "bots": [_bot_entry(b, race, now_price, with_luck) for b in NEW_BOTS],
            "reference": _bot_entry(REFERENCE_BOT, race, now_price, with_luck),
            "benchmark": _benchmark(race, now_price)}


def standings(rows: List[dict], with_luck: bool = True) -> dict:
    """The table. with_luck=False skips the shifted null, which costs seconds.

    "bots" is cohort 1 (bots 1 to 6) exactly as before, each with cohort 1
    added. "cohort2" is the separate section for bots 7 to 16 and the reference.
    """
    race = race_rows(rows)
    if not race:
        return {"started": False, "bots": [], "benchmark": None,
                "race_rows": 0, "race_start": "",
                "cohort2": _cohort2_section(rows, with_luck)}

    now_price = last_price(race)
    bots = [_bot_entry(bot, race, now_price, with_luck) for bot in BOTS]

    return {"started": True, "bots": bots, "benchmark": _benchmark(race, now_price),
            "race_rows": len(race), "race_start": str(race[0].get("run_utc", "")),
            "generated_utc": str(race[-1].get("run_utc", "")),
            "cohort2": _cohort2_section(rows, with_luck)}


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


def ranked_cohort2(section: dict) -> List[dict]:
    """Cohort 2 bots, best balance first, ties in bot number order. The
    reference is not ranked: it is the yardstick, not a bot."""
    return sorted(section["bots"], key=lambda b: -b["balance_now"])


TAB_HEADER = ["rank", "bot", "$100 is now", "trades", "winners", "total_usd", "total_pct", "worst_usd",
              "open_side", "open_entry", "open_unrealised_usd", "luck_p", "luck_note", "cohort"]


def _tab_row(rank, bot: dict) -> list:
    o = bot["open"] or {}
    return [rank, bot["label"], bot["balance_now"], bot["trades"], bot["winners"], bot["total_usd"],
            bot["total_pct"], bot["worst_usd"], o.get("side", "flat"),
            o.get("entry_price", ""), o.get("unrealised_usd", ""),
            bot["luck_p"] if bot["luck_p"] is not None else "", bot["luck_note"], bot.get("cohort", 1)]


def write_tab(client, table: dict, updated_utc: Optional[str] = None) -> None:
    """Overwrite the leaderboard tab: header, bots in rank order, blank row,
    benchmark row, updated_utc. Cohort 2 follows below as its own block: a
    title row, the same header, its bots in rank order, then the reference."""
    rows = [list(TAB_HEADER)]
    for rank, bot in enumerate(ranked(table), start=1):
        rows.append(_tab_row(rank, bot))
    rows.append([])
    b = table["benchmark"] or {}
    rows.append(["benchmark", "Buy and hold", b.get("balance_now") or "", "", "", b.get("usd", ""), b.get("pct", ""), "",
                 "", b.get("first_price", ""), b.get("last_price", ""), "", "first and last price", ""])
    rows.append(["updated_utc", updated_utc or datetime.now(timezone.utc).isoformat()])

    c2 = table.get("cohort2")
    if c2:
        rows.append([])
        rows.append(["cohort 2", "Bots 7 to 16, racing from the first row with a context_version",
                     c2["state"], "", "", "", "", "", "", "", "", "",
                     "started %s, %d rows" % (c2["start_utc"], c2["rows"]) if c2["started"] else c2["note"], 2])
        rows.append(list(TAB_HEADER))
        for rank, bot in enumerate(ranked_cohort2(c2), start=1):
            rows.append(_tab_row(rank if c2["started"] else "", bot))
        ref = c2["reference"]
        row = _tab_row("reference", dict(ref, label="%s (reference, not a bot)" % ref["label"]))
        rows.append(row)
    # The tab was first made 30 x 12; the cohort 2 block needs more room.
    _ensure_grid(client, TAB, len(rows), len(TAB_HEADER))
    client.write_table(TAB, rows)


# ------------------------------------------------------- every bot trade --

BOT_TRADE_COLUMNS = ["bot", "cohort", "status", "side", "entry_utc", "entry_price", "entry_score",
                     "exit_utc", "exit_price", "exit_reason", "pnl_pct", "size", "pnl_pct_sized",
                     "hours_held", "stop_pct", "ma200", "vol20_pct", "event_flags", "mfe_pct", "mae_pct"]


def _context_by_row(rows: List[dict]) -> List[tuple]:
    """(ma200, vol20_pct, event_flags text) in force after each row, by the
    same carry forward rule the replay uses."""
    carry = replay.ContextCarry()
    out = []
    for row in rows:
        if parse_utc(row.get("run_chicago", "")) is not None:
            carry.observe(row)
        out.append((carry.ma200, carry.vol20_pct,
                    ";".join(sorted(carry.flags)) if carry.flags else ""))
    return out


def _excursions(rows: List[dict], first: int, last: int, side: str, entry_price: float) -> Tuple[float, float]:
    """(max favourable, max adverse) in percent of the entry price, at the rows
    from entry to exit that have a price. Before the spread. Adverse is <= 0."""
    best = worst = 0.0
    for row in rows[first:last + 1]:
        price = to_float(row.get("brent_price"))
        if price is None or not entry_price:
            continue
        move = (price - entry_price) / entry_price * 100.0
        if side == "short":
            move = -move
        best, worst = max(best, move), min(worst, move)
    return round(best, 4), round(worst, 4)


def _hours_between(start_utc, end_utc) -> object:
    a, b = parse_utc(str(start_utc or "")), parse_utc(str(end_utc or ""))
    return round((b - a).total_seconds() / 3600.0, 1) if (a and b) else ""


def bot_trade_records(bot: dict, rows: List[dict]) -> List[dict]:
    """Every closed trade, then the open position if any, for one bot over its
    own rows. entry_score is the score the bot acted on, so negated for the
    opposite bots."""
    trades, position, info = run_bot_full(bot, rows)
    index = {str(r.get("run_utc", "")): i for i, r in enumerate(rows)}
    context = _context_by_row(rows)
    now_price = last_price(rows)
    out = []

    def record(status, t, exit_utc, exit_price, reason, pnl_pct, hours):
        i0 = index.get(str(t["entry_utc"]), 0)
        i1 = index.get(str(exit_utc), len(rows) - 1) if status == "closed" else len(rows) - 1
        mfe, mae = _excursions(rows, i0, i1, t["side"], t["entry_price"])
        ma200, vol, flags = context[i0] if context else (None, None, "")
        size = t.get("size", 1.0)
        out.append({
            "bot": bot["key"], "cohort": bot.get("cohort", 1), "status": status, "side": t["side"],
            "entry_utc": t["entry_utc"], "entry_price": t["entry_price"], "entry_score": t.get("entry_score", ""),
            "exit_utc": exit_utc, "exit_price": exit_price, "exit_reason": reason,
            "pnl_pct": pnl_pct, "size": size, "pnl_pct_sized": round(pnl_pct * size, 4),
            "hours_held": hours,
            "stop_pct": "" if (bot.get("calendar") or bot.get("trend_only")) else t.get("stop_pct", config.TRAILING_STOP_PCT),
            "ma200": "" if ma200 is None else ma200, "vol20_pct": "" if vol is None else vol,
            "event_flags": flags, "mfe_pct": mfe, "mae_pct": mae,
        })

    for t in trades:
        hours = t.get("hours", "")
        if hours == "":
            hours = _hours_between(t["entry_utc"], t["exit_utc"])
        record("closed", t, t["exit_utc"], t["exit_price"], t["reason"], t["pnl_pct"], hours)
    if position.is_open:
        open_info = info.get("open") or {}
        t = {"side": position.side, "entry_utc": position.entry_utc, "entry_price": position.entry_price,
             "entry_score": position.entry_score}
        t.update({k: v for k, v in open_info.items() if k in ("stop_pct", "size")})
        last_utc = str(rows[-1].get("run_utc", "")) if rows else ""
        pct = (rules.trade_pnl(position.side, position.entry_price, now_price,
                               parse_utc(position.entry_utc or ""), parse_utc(last_utc))[1]
               if now_price is not None else "")
        record("open", t, "", now_price if now_price is not None else "", "open",
               pct, _hours_between(position.entry_utc, last_utc))
        if pct == "":
            out[-1]["pnl_pct_sized"] = ""
    return out


def bot_trades_report(rows: List[dict]) -> dict:
    """All 16 bots plus the reference: each over its own cohort's rows."""
    race = race_rows(rows)
    c2 = cohort2_rows(rows)
    records = []
    for bot in BOTS:
        records.extend(bot_trade_records(bot, race))
    for bot in NEW_BOTS + [REFERENCE_BOT]:
        records.extend(bot_trade_records(bot, c2) if c2 else [])
    return {"generated_utc": str(race[-1].get("run_utc", "")) if race else "",
            "race_rows": len(race), "cohort2_rows": len(c2),
            "cohort2_state": "running" if c2 else "not started",
            "columns": BOT_TRADE_COLUMNS, "trades": records}


def _ensure_grid(client, tab: str, n_rows: int, n_cols: int) -> None:
    """Best effort: make sure a real sheet's tab is big enough before a wide
    write. A no op for the fakes and the dry run, and never fatal."""
    sheet = getattr(client, "_sheet", None)
    if sheet is None:
        return
    try:
        try:
            ws = client.worksheet(tab)
        except Exception:  # noqa: BLE001 - missing tab: create it at the right size
            ws = sheet.add_worksheet(title=tab, rows=max(n_rows + 100, 200), cols=max(n_cols, 26))
            client._tabs[tab] = ws
            return
        if ws.row_count < n_rows + 20 or ws.col_count < n_cols:
            ws.resize(rows=max(ws.row_count, n_rows + 100), cols=max(ws.col_count, n_cols))
    except Exception:  # noqa: BLE001 - the write below reports its own failure
        pass


def write_bot_trades_tab(client, report: dict, updated_utc: Optional[str] = None) -> None:
    """Overwrite the bot_trades tab: header, one row per trade or open
    position, blank row, updated_utc."""
    rows = [list(BOT_TRADE_COLUMNS)]
    for t in report["trades"]:
        rows.append([t[c] for c in BOT_TRADE_COLUMNS])
    rows.append([])
    rows.append(["updated_utc", updated_utc or datetime.now(timezone.utc).isoformat()])
    _ensure_grid(client, BOT_TRADES_TAB, len(rows), len(BOT_TRADE_COLUMNS))
    client.write_table(BOT_TRADES_TAB, rows)


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
    c2 = table.get("cohort2")
    if c2:
        if not c2["started"]:
            print("\n  cohort 2 (bots 7 to 16): not started, %s" % c2["note"])
        else:
            print("\n  cohort 2 (bots 7 to 16), %d rows from %s" % (c2["rows"], c2["start_utc"]))
            for rank, bot in enumerate(ranked_cohort2(c2), start=1):
                print("  %-4d %-32s %11.2f %6d %8d %12.2f %8.2f%% %10.2f  %-26s %s  missing %d"
                      % (rank, bot["label"], bot["balance_now"], bot["trades"], bot["winners"], bot["total_usd"],
                         bot["total_pct"], bot["worst_usd"], _open_text(bot), _luck_text(bot),
                         bot["missing_input_decisions"]))
            ref = c2["reference"]
            print("  ref  %-32s %11.2f %6d %8d %12.2f %8.2f%% %10.2f  %-26s %s  (reference, not a bot)"
                  % (ref["label"], ref["balance_now"], ref["trades"], ref["winners"], ref["total_usd"],
                     ref["total_pct"], ref["worst_usd"], _open_text(ref), _luck_text(ref)))
    print("\n  With 16 bots, one will lead by luck, and Bot 1 will often be it. "
          "A winner has to repeat in a second period.")


def _write_json(name: str, data: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print("wrote %s" % path)


def main() -> int:
    client = sheets.open_client(dry_run=False)
    rows = client.records(SIGNALS)
    table = standings(rows)
    if not table["started"]:
        print("race has not started: no v2 rows yet")
        return 0
    print_table(table)

    _write_json("leaderboard.json", table)
    report = bot_trades_report(rows)
    _write_json("bot_trades.json", report)

    try:
        write_tab(client, table)
    except Exception as exc:  # noqa: BLE001 - the printout and the file are the deliverable
        print("leaderboard tab not written: %s" % exc)
    try:
        write_bot_trades_tab(client, report)
    except Exception as exc:  # noqa: BLE001
        print("bot_trades tab not written: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
