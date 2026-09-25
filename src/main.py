"""One hourly run of the oil news signal logger.

Log only. No broker, no real orders. Paper positions exist as rows in a Google
Sheet and nothing else.

    python -m src.main             # live run
    python -m src.main --dry-run   # prints everything, writes nothing
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import List, Optional

from . import config, news, outcomes, price as price_client, rules, scorer, sheets, weekend
from .rules import FLAT, Position
from .sheets import HEADLINES, SIGNALS, STATE, TRADES


def _as_bool(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _float_or_none(value) -> Optional[float]:
    return outcomes.to_float(value)


def state_to_position(state: dict) -> Position:
    side = (state.get("position") or "flat").strip().lower()
    if side not in ("long", "short"):
        return FLAT
    return Position(
        side=side,
        entry_price=_float_or_none(state.get("entry_price")),
        entry_utc=(state.get("entry_utc") or "").strip(),
        entry_score=int(_float_or_none(state.get("entry_score")) or 0),
        best_price=_float_or_none(state.get("best_price")),
        trade_id=(state.get("trade_id") or "").strip(),
    )


def position_to_state(position: Position, now_utc: datetime) -> dict:
    if not position.is_open:
        row = dict(sheets.EMPTY_STATE)
        row["last_run_utc"] = now_utc.isoformat()
        return row
    return {
        "position": position.side,
        "entry_price": position.entry_price if position.entry_price is not None else "",
        "entry_utc": position.entry_utc or "",
        "entry_score": position.entry_score if position.entry_score is not None else "",
        "best_price": position.best_price if position.best_price is not None else "",
        "trade_id": position.trade_id or "",
        "last_run_utc": now_utc.isoformat(),
    }


def make_trade_id(side: str, now_utc: datetime) -> str:
    return "T-%s-%s" % (now_utc.strftime("%Y%m%dT%H%M"), side[0].upper())


def carry_forward(recent_signals: List[dict]) -> dict:
    """Reuse the last hour's score when no new headlines arrived."""
    for row in reversed(recent_signals):
        if str(row.get("score", "")).strip() != "":
            return {
                "score": int(float(row["score"])),
                "confidence": (row.get("confidence") or "low").strip(),
                "direction": (row.get("direction") or "neutral").strip(),
                "key_headlines": "",
                "new_information": False,
                "reasoning": "carried from %s" % row.get("run_utc", "previous run"),
            }
    return {
        "score": 0,
        "confidence": "low",
        "direction": "neutral",
        "key_headlines": "",
        "new_information": False,
        "reasoning": "no prior score to carry",
    }


def weekend_rows_for(all_signals: List[dict], key: str) -> List[dict]:
    """Signal rows inside one weekend shutdown.

    Deliberately NOT "any row where the market was closed": Monday has a 16:00
    to 17:00 Chicago maintenance break, and weekend_key still resolves to the
    Friday that started the window, so a Monday evening run would be counted
    into the weekend scores it has nothing to do with.
    """
    out = []
    for row in all_signals:
        if _as_bool(row.get("market_open")):
            continue
        when = outcomes.parse_utc(row.get("run_chicago", ""))
        if when is None:
            continue
        local = when.astimezone(rules.CHICAGO)
        if not rules.is_weekend_shutdown(local):
            continue
        if weekend.weekend_key(local) == key:
            out.append(row)
    return out


def run(dry_run: bool = False) -> int:
    now_utc = datetime.now(timezone.utc)
    now_chicago = rules.to_chicago(now_utc)
    market_open = rules.is_market_open(now_chicago)
    notes: List[str] = []

    print("run %s (chicago %s) market_open=%s" % (now_utc.isoformat(), now_chicago.isoformat(), market_open))

    client = sheets.open_client(dry_run=dry_run)

    # 1-2. News, cleaned and deduped against what we have already logged.
    known_hashes = client.recent_hashes(config.DEDUPE_LOOKBACK_ROWS)
    fresh = news.collect(known_hashes, now_utc)
    print("new headlines: %d" % len(fresh))

    # 3. Brent price. A blank price never stops the run.
    quote = price_client.fetch_brent()
    if quote.note:
        notes.append(quote.note)
    print("brent: %s" % (quote.price if quote.ok else "unavailable"))

    # 4. Context.
    all_signals = client.records(SIGNALS)
    recent_signals = all_signals[-config.CONTEXT_SIGNAL_ROWS:]
    position = state_to_position(client.get_state())

    # 5. Score.
    scored = None
    usage = None
    if fresh:
        scored, usage = scorer.score_news([h.as_dict() for h in fresh], recent_signals)
        if usage:
            notes.append(usage.as_note())
        if scored is None:
            notes.append(usage.error or "scorer_unavailable")
    else:
        notes.append("no_new_news")

    if scored is not None:
        score_fields = {
            "score": scored.score,
            "confidence": scored.confidence,
            "direction": scored.direction,
            "key_headlines": " | ".join(scored.key_headlines),
            "new_information": scored.new_information,
            "reasoning": scored.reasoning,
        }
    elif fresh:
        # Both attempts produced bad JSON: log an error row, skip trade logic.
        score_fields = {
            "score": "", "confidence": "", "direction": "",
            "key_headlines": "", "new_information": "", "reasoning": "scoring failed",
        }
    else:
        score_fields = carry_forward(recent_signals)

    # 6. Rules.
    if scored is None and fresh:
        decision = rules.Decision(action="error", position_after=position, notes="scoring_error")
    else:
        decision = rules.decide(
            score=score_fields["score"] if score_fields["score"] != "" else None,
            confidence=score_fields["confidence"] or None,
            position=position,
            now_chicago=now_chicago,
            price=quote.price,
        )
    if decision.notes:
        notes.append(decision.notes)

    new_position = decision.position_after
    trade_row = None

    if decision.action in ("buy", "sell"):
        new_position = Position(
            side=new_position.side,
            entry_price=new_position.entry_price,
            entry_utc=now_utc.isoformat(),
            entry_score=new_position.entry_score,
            best_price=new_position.best_price,
            trade_id=make_trade_id(new_position.side, now_utc),
        )
    elif decision.action == "close" and position.is_open:
        exit_price = quote.price if quote.ok else position.best_price
        pnl_usd, pnl_pct = rules.pnl(position.side, position.entry_price or 0.0, exit_price or 0.0)
        entry_at = outcomes.parse_utc(position.entry_utc or "")
        hours_held = round((now_utc - entry_at).total_seconds() / 3600.0, 2) if entry_at else ""
        trade_row = {
            "trade_id": position.trade_id,
            "side": position.side,
            "entry_utc": position.entry_utc,
            "entry_price": position.entry_price,
            "entry_score": position.entry_score,
            "exit_utc": now_utc.isoformat(),
            "exit_price": exit_price,
            "exit_reason": decision.exit_reason or "manual",
            "best_price": position.best_price,
            "pnl_usd_bbl": pnl_usd,
            "pnl_pct": pnl_pct,
            "hours_held": hours_held,
        }

    # 7. Write.
    if fresh:
        client.append(
            HEADLINES,
            [dict(h.as_dict(), run_utc=now_utc.isoformat()) for h in fresh],
        )

    signal_row = {
        "run_utc": now_utc.isoformat(),
        "run_chicago": now_chicago.isoformat(),
        "rules_version": config.RULES_VERSION,
        "market_open": market_open,
        "brent_price": quote.price if quote.ok else "",
        "price_time_utc": quote.price_time_utc,
        "new_headline_count": len(fresh),
        "action": decision.action,
        "position_after": new_position.side,
        "notes": "; ".join(n for n in notes if n),
        "price_1h": "", "price_4h": "", "price_24h": "", "price_72h": "",
        "move_1h_pct": "", "move_4h_pct": "", "move_24h_pct": "", "move_72h_pct": "",
    }
    signal_row.update(score_fields)
    client.append(SIGNALS, [signal_row])
    client.write_state(position_to_state(new_position, now_utc))

    if trade_row:
        client.append(TRADES, [trade_row])
        print("closed %s %s pnl %s/bbl" % (trade_row["side"], trade_row["trade_id"], trade_row["pnl_usd_bbl"]))

    # 8. Weekend bookkeeping.
    key = weekend.weekend_key(now_chicago)
    if key:
        if rules.is_friday_close_run(now_chicago):
            weekend.on_friday_close(
                client,
                now_chicago,
                quote.price,
                score_fields["score"] if score_fields["score"] != "" else None,
                position.side if trade_row and trade_row["exit_reason"] == "friday" else "none",
                trade_row["exit_price"] if trade_row and trade_row["exit_reason"] == "friday" else None,
            )
        elif rules.is_weekend_shutdown(now_chicago):
            weekend.on_weekend_run(
                client,
                now_chicago,
                weekend_rows_for(all_signals, key) + [signal_row],
                all_signal_rows=all_signals + [signal_row],
            )
        else:
            weekend.on_open_run(client, now_chicago, now_utc, quote.price)

    # 9. Outcomes backfill.
    touched = outcomes.backfill(client, now_utc)
    print("outcome rows backfilled: %d" % touched)

    print("action=%s position=%s" % (decision.action, new_position.side))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Oil news signal logger, one hourly run.")
    parser.add_argument("--dry-run", action="store_true", help="print everything, write nothing")
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
