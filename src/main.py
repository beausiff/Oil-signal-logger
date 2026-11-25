"""One hourly run of the oil news signal logger.

Paper positions live as rows in a Google Sheet. With BROKER_MODE=demo the same
position is mirrored onto an OANDA practice account (fake money, see src/broker.py).
There is no live trading mode.

    python -m src.main             # live run
    python -m src.main --dry-run   # prints everything, writes nothing
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

from . import broker, config, context, news, outcomes, price as price_client, rules, scorer, sheets, weekend
from .rules import FLAT, Position
from .sheets import CONTEXT, HEADLINES, SIGNALS, STATE, TRADES


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
    # Nothing to carry: leave the score blank rather than invent a 0. The
    # rules treat a blank score as "no signal" (no entry, no reversal exit).
    return {
        "score": "",
        "confidence": "",
        "direction": "",
        "key_headlines": "",
        "new_information": False,
        "reasoning": "no prior score to carry",
    }


def last_logged_price(rows: List[dict]) -> Optional[float]:
    """The most recent Brent price in the signals rows, or None."""
    for row in reversed(rows):
        value = outcomes.to_float(row.get("brent_price"))
        if value is not None:
            return value
    return None


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


def scoring_failed(row: dict) -> bool:
    """Did this row's scorer call fail, and is it still unscored?

    A backfilled row keeps action "error" but has a score, so it is not a
    current failure; a row with no new headlines never called the scorer.
    """
    return (
        str(row.get("score", "")).strip() == ""
        and (outcomes.to_float(row.get("new_headline_count")) or 0) > 0
    )


def had_headlines(row: dict) -> bool:
    """Rows with no new headlines never called the scorer, so say nothing about it."""
    return outcomes.to_float(row.get("new_headline_count")) != 0


def news_window_minutes(gap: Optional[float]) -> int:
    """Minutes of news to accept: everything since the previous run, plus a margin.

    Clamped to [NEWS_LOOKBACK_MINUTES, NEWS_LOOKBACK_MAX_MINUTES]. With no
    previous run (gap is None) it is the minimum.
    """
    if gap is None:
        return config.NEWS_LOOKBACK_MINUTES
    window = int(round(gap + config.NEWS_WINDOW_MARGIN_MINUTES))
    return max(config.NEWS_LOOKBACK_MINUTES, min(config.NEWS_LOOKBACK_MAX_MINUTES, window))


def minutes_since_last_run(state: dict, now_utc: datetime) -> Optional[float]:
    """None when there is no previous run to compare against."""
    last = outcomes.parse_utc((state.get("last_run_utc") or "").strip())
    if last is None:
        return None
    return (now_utc - last).total_seconds() / 60.0


def _ensure_columns(client, tab: str) -> None:
    """Add header cells for new schema columns (and create a missing tab).

    One cheap read per tab per run. Fail soft: a sheet that cannot be patched
    here still gets its row appended, and `python -m scripts.init_sheet` fixes
    the header later.
    """
    ensure = getattr(client, "ensure_columns", None)
    if ensure is None:
        return
    try:
        for change in ensure(tab):
            print("sheet: %s" % change)
    except Exception as exc:  # noqa: BLE001
        print("sheet header check skipped for %s: %s" % (tab, type(exc).__name__))


def run(dry_run: bool = False, force: bool = False) -> int:
    now_utc = datetime.now(timezone.utc)
    started = time.monotonic()
    now_chicago = rules.to_chicago(now_utc)
    market_open = rules.is_market_open(now_chicago)
    notes: List[str] = []

    print("run %s (chicago %s) market_open=%s" % (now_utc.isoformat(), now_chicago.isoformat(), market_open))

    client = sheets.open_client(dry_run=dry_run)

    # 0. Is this attempt the one that counts this hour?
    #
    # GitHub delivers only a few scheduled runs a day, so a gap this short
    # means two runs landed close together (a manual or external dispatch next
    # to a scheduled one). The second stops here, before any news, price or
    # model call, so a double fire costs one read of the state tab, not a row.
    gap = minutes_since_last_run(client.get_state(), now_utc)
    if not force and gap is not None and gap < config.MIN_MINUTES_BETWEEN_RUNS:
        print("skipping: last run was %.0f minutes ago, minimum gap is %d"
              % (gap, config.MIN_MINUTES_BETWEEN_RUNS))
        return 0

    # 1-2. News, cleaned and deduped against what we have already logged.
    known_hashes = client.recent_hashes(config.DEDUPE_LOOKBACK_ROWS)
    window = news_window_minutes(gap)
    notes.append("news_window_min=%d" % window)
    news.reset_stats()
    fresh = news.collect(known_hashes, now_utc, lookback_minutes=window)
    print("news window: %d minutes, new headlines: %d" % (window, len(fresh)))

    # 3. Brent price. A blank price never stops the run.
    price_started = time.monotonic()
    quote = price_client.fetch_brent()
    price_ms = int((time.monotonic() - price_started) * 1000)
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
        scored, usage = scorer.score_news(
            [h.as_dict() for h in fresh], recent_signals, window_minutes=window)
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
        # The Friday close, the late weekend close and the trailing stop need a
        # price, not a score, so they still apply. A blank score blocks entries
        # and the reversal exit. Anything but a close is logged as an error.
        decision = rules.decide(
            score=None, confidence=None, position=position,
            now_chicago=now_chicago, price=quote.price,
        )
        if decision.action != "close":
            decision = rules.Decision(
                action="error", position_after=decision.position_after, notes="scoring_error",
            )
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
        exit_price = quote.price if quote.ok else last_logged_price(all_signals)
        if exit_price is None:
            exit_price = position.entry_price
        # What decide ratcheted to on this run, which it does not hand back on a close.
        peak = (rules.update_best_price(position, quote.price).best_price
                if market_open and quote.ok else position.best_price)
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
            "best_price": peak,
            "pnl_usd_bbl": pnl_usd,
            "pnl_pct": pnl_pct,
            "hours_held": hours_held,
        }

    # 6b. Mirror onto the OANDA practice account. Off by default, never fails the run.
    # Skipped while the market is shut: the next open run catches up.
    if not dry_run and market_open:
        broker_note = broker.sync(new_position.side, allow_open=decision.action in ("buy", "sell"))
        if broker_note:
            notes.append(broker_note)
            print(broker_note)

    # 6c. Context for the log (src/context.py). Strictly fail soft and after
    # the decision: nothing it does can reach the score, the rules or the
    # columns that existed before it. Any error leaves blanks.
    gathered = None
    gather_errors: List[str] = []
    try:
        gathered = context.gather(now_utc, quote.price if quote.ok else None, all_signals)
    except Exception as exc:  # noqa: BLE001 - never fail the run over context
        gather_errors.append("gather_failed:%s" % type(exc).__name__)
        print("context skipped: %s" % type(exc).__name__)

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
        # "carried" marks a row that reused an earlier score, so the analyses
        # can tell it from one the model scored this hour.
        "score_source": "live" if scored is not None else ("" if fresh else "carried"),
    }
    signal_row.update(score_fields)
    signal_row.update(context.signal_fields(now_utc, gathered))
    if not dry_run:
        _ensure_columns(client, SIGNALS)
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
                # A close the Friday run never made, taken at the first
                # weekend run instead, still belongs on the weekend row.
                closed_side=position.side if trade_row and trade_row["exit_reason"] == "friday" else "none",
                exit_price=trade_row["exit_price"] if trade_row and trade_row["exit_reason"] == "friday" else None,
            )

    # Every run, whatever day it is: a weekend row left unfinished because its
    # Monday runs were dropped must still get finished.
    completed = weekend.complete_pending(client, now_utc, price=quote.price)
    if completed:
        print("weekend rows completed: %d" % completed)

    # 9. Outcomes backfill.
    touched = outcomes.backfill(client, now_utc)
    print("outcome rows backfilled: %d" % touched)

    print("action=%s position=%s" % (decision.action, new_position.side))

    # Last, so nothing above is lost: a run that scored nothing is still a run
    # whose headlines, price and backfill are worth keeping.
    written = all_signals + [signal_row]

    # The context tab: one wide row per run, after everything else, in its own
    # try/except so a failure here costs that row and nothing more.
    try:
        if not dry_run:
            _ensure_columns(client, CONTEXT)
        client.append(CONTEXT, [context.build_row(
            now_utc,
            gathered,
            context.news_columns(news.LAST_STATS, fresh, now_utc, window),
            context.run_meta_columns(
                now_utc, usage, quote, price_ms,
                int((time.monotonic() - started) * 1000)),
            extra_errors=gather_errors,
        )])
    except Exception as exc:  # noqa: BLE001 - never fail the run over a side table
        print("context row skipped: %s: %s" % (type(exc).__name__, exc))

    # The race leaderboard, from rows already in memory. It must never fail or
    # slow a run, so it skips the shifted null and swallows any error.
    if not dry_run:
        try:
            from scripts import leaderboard

            table = leaderboard.standings(written, with_luck=False)
            if table["started"]:
                leaderboard.write_tab(client, table)
        except Exception as exc:  # noqa: BLE001 - never fail the run over a side table
            print("leaderboard skipped: %s: %s" % (type(exc).__name__, exc))
        # Every trade of every bot, refreshed each run so the log is never stale.
        try:
            from scripts import leaderboard

            if leaderboard.race_rows(written):
                leaderboard.write_bot_trades_tab(client, leaderboard.bot_trades_report(written))
        except Exception as exc:  # noqa: BLE001 - never fail the run over a side table
            print("bot_trades skipped: %s: %s" % (type(exc).__name__, exc))

    # Rows with no new headlines never called the scorer, so a quiet run in
    # the middle of an outage must not reset the streak.
    scoring_rows = [r for r in written if had_headlines(r)]
    recent = scoring_rows[-config.ALERT_AFTER_CONSECUTIVE_ERRORS:]
    if len(recent) == config.ALERT_AFTER_CONSECUTIVE_ERRORS and all(
        scoring_failed(r) for r in recent
    ):
        print(
            "FAILING THE JOB: %d consecutive runs scored nothing. Everything else "
            "was written. Check the ANTHROPIC secret."
            % config.ALERT_AFTER_CONSECUTIVE_ERRORS
        )
        return 1

    priced = written[-config.ALERT_AFTER_CONSECUTIVE_BLANK_PRICES:]
    if len(priced) == config.ALERT_AFTER_CONSECUTIVE_BLANK_PRICES and all(
        str(r.get("brent_price", "")).strip() == "" for r in priced
    ):
        print(
            "FAILING THE JOB: %d consecutive runs logged no Brent price. "
            "Check the OILTRACKER secret and the OilPriceAPI account."
            % config.ALERT_AFTER_CONSECUTIVE_BLANK_PRICES
        )
        return 1

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Oil news signal logger, one hourly run.")
    parser.add_argument("--dry-run", action="store_true", help="print everything, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="run even if the last run was recent")
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
