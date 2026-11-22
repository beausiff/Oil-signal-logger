"""Emit everything needed to tell the story of the replayed trades, as JSON.

    python -m scripts.trade_story

The price series, the trades the locked rules would have taken, and for each
entry and exit the score and the model's own words at that moment. Written for
a chart, so it is deliberately compact: reasoning is trimmed and only the rows
that mattered carry their text.
"""

from __future__ import annotations

import json
import os
import sys

from src import rules, sheets
from src.outcomes import to_float, parse_utc
from src.sheets import SIGNALS
from scripts.replay import replay, calendar_only

OUT_DIR = "backup"


def trim(text, limit=320):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def main() -> int:
    client = sheets.open_client(dry_run=False)
    rows = client.records(SIGNALS)
    rows.sort(key=lambda r: str(r.get("run_utc", "")))

    by_run = {str(r.get("run_utc", "")).strip(): r for r in rows}
    trades, open_position, _ = replay(rows)

    def decorate(utc):
        row = by_run.get(str(utc).strip(), {})
        return {
            "score": to_float(row.get("score")),
            "confidence": row.get("confidence", ""),
            "direction": row.get("direction", ""),
            "reasoning": trim(row.get("reasoning")),
            "headlines": trim(row.get("key_headlines"), 260),
            "backfilled": str(row.get("score_source", "")).startswith("backfilled"),
        }

    out = {
        "generated_utc": rows[-1].get("run_utc", "") if rows else "",
        "rules": {
            "version": __import__("src.config", fromlist=["x"]).RULES_VERSION,
            "entry": __import__("src.config", fromlist=["x"]).ENTRY_LONG_SCORE,
            "stop_pct": __import__("src.config", fromlist=["x"]).TRAILING_STOP_PCT,
            "spread": __import__("src.config", fromlist=["x"]).SPREAD_USD_PER_BBL,
        },
        "prices": [
            {"t": str(r.get("run_utc", ""))[:16], "p": to_float(r.get("brent_price")),
             "s": to_float(r.get("score"))}
            for r in rows if to_float(r.get("brent_price")) is not None
        ],
        "trades": [
            {
                "side": t["side"], "reason": t["reason"],
                "entry_utc": str(t["entry_utc"])[:16], "entry_price": t["entry_price"],
                "exit_utc": str(t["exit_utc"])[:16], "exit_price": t["exit_price"],
                "pnl_pct": t["pnl_pct"], "pnl_usd": t["pnl_usd"], "hours": t["hours"],
                "real": not decorate(t["entry_utc"])["backfilled"],
                "entry": decorate(t["entry_utc"]),
                "exit": decorate(t["exit_utc"]),
            }
            for t in trades
        ],
        "control": [
            {"entry_utc": str(t["entry_utc"])[:16], "entry_price": t["entry_price"],
             "exit_utc": str(t["exit_utc"])[:16], "exit_price": t["exit_price"],
             "pnl_pct": t["pnl_pct"], "pnl_usd": t["pnl_usd"]}
            for t in calendar_only(rows)
        ],
        "open": {"side": open_position.side, "entry": open_position.entry_price}
        if open_position.is_open else None,
    }
    # Written to a file, not printed: GitHub's log API both replaces braces
    # with asterisks and truncates long lines, so nothing structured survives
    # a round trip through it. The file is committed with the backups, which
    # makes it durable and readable without running anything.
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "trade_story.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print("wrote %s: %d prices, %d trades, %d control trades"
          % (path, len(out["prices"]), len(out["trades"]), len(out["control"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
