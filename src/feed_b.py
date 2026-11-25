"""Feed B: the same six bot rules on NewsAPI.ai headlines, paper only.

    python -m src.feed_b            # one run (workflow feed_b, after every hourly run)

Fully isolated from the main hourly run: it only READS the `signals` tab, and
writes its own tabs (`signals_b`, `headlines_b`, `feed_b_state`, `leaderboard_b`).
It never touches the broker, the `state` tab or the `trades` tab, and it never
fails the job: every error is printed (redacted) and the exit code is 0.

One run:
  a. take the LATEST main row (rules_version v2, run_utc >= the feed B start);
     if signals_b already has a row for its run_utc, there is nothing to do and
     nothing is spent. Older unmatched rows are never backfilled.
  b. news window = the live rule: minutes since the previous feed B row + 10,
     clamped to [90, 720], first run 90, ending at that main row's run_utc.
  c. fetch from NewsAPI.ai (at most 2 requests, 1,500 for the life of feed B).
  d. score with src.scorer.score_news, context = feed B's own previous rows.
  e. prices, times and market_open are copied from the main row, the base rules
     (src.rules.decide) are applied to feed B's own position, and one row is
     written to signals_b (+ headlines_b).
  f. the standings and the paired comparison are rewritten (scripts.leaderboard_b).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from . import config, main as live, news_b, outcomes, rules, scorer, sheets
from .rules import FLAT
from .sheets import HEADLINES, SIGNALS

SIGNALS_B = "signals_b"
HEADLINES_B = "headlines_b"
STATE_B = "feed_b_state"
LEADERBOARD_B = "leaderboard_b"
FEED_MARK = "B"

# signals_b has the signals columns unchanged, plus ONE column appended at the end.
SIGNALS_B_SCHEMA: List[str] = list(sheets.SCHEMAS[SIGNALS]) + ["feed"]
HEADLINES_B_SCHEMA: List[str] = list(sheets.SCHEMAS[HEADLINES])
STATE_KEYS = ["start_utc", "requests_used", "last_main_run_utc", "position", "entry_price",
              "entry_utc", "entry_score", "best_price", "trade_id", "last_run_utc"]
SCHEMAS_B: Dict[str, List[str]] = {
    SIGNALS_B: SIGNALS_B_SCHEMA,
    HEADLINES_B: HEADLINES_B_SCHEMA,
}

RACE_VERSION_PREFIX = "v2"
ROW_VERSION = "v2"
# A main row older than this is not reconstructable cheaply (newest first paging
# from now, 2 requests), so it is left unmatched instead of spending requests on it.
MAX_ROW_AGE_MINUTES = 180


# ------------------------------------------------------------- the sheet ----

class SheetStore:
    """feed B's view of the sheet. The only place that touches gspread for feed B."""

    def __init__(self, client=None):
        self.client = client or sheets.SheetsClient()
        self._sheet = self.client._sheet

    def _find(self, tab: str):
        import gspread
        try:
            return self._sheet.worksheet(tab)
        except gspread.WorksheetNotFound:
            return None

    def main_rows(self) -> List[dict]:
        return self.client.records(SIGNALS)

    def rows(self, tab: str) -> List[dict]:
        ws = self._find(tab)
        if ws is None:
            return []
        values = ws.get_all_values()
        if len(values) < 2:
            return []
        header = values[0]
        return [{header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
                for row in values[1:]]

    def ensure_tabs(self) -> None:
        for tab, header in SCHEMAS_B.items():
            ws = self._find(tab)
            if ws is None:
                ws = self._sheet.add_worksheet(title=tab, rows=2000, cols=max(len(header), 10))
            if ws.row_values(1) != header:
                ws.update(range_name="A1:%s1" % sheets.column_letter(len(header) - 1), values=[header])
        if self._find(STATE_B) is None:
            self._sheet.add_worksheet(title=STATE_B, rows=40, cols=4)

    def append(self, tab: str, rows: Sequence[dict]) -> None:
        if not rows:
            return
        header = SCHEMAS_B[tab]
        payload = [["" if r.get(c) is None else sheets.neutralise(r.get(c, "")) for c in header] for r in rows]
        self._find(tab).append_rows(payload, value_input_option="USER_ENTERED", table_range="A1")

    def known_hashes(self, count: int) -> List[str]:
        ws = self._find(HEADLINES_B)
        if ws is None:
            return []
        column = HEADLINES_B_SCHEMA.index("hash") + 1
        return ws.col_values(column)[1:][-count:]

    def get_state(self) -> dict:
        ws = self._find(STATE_B)
        state = {k: "" for k in STATE_KEYS}
        if ws is None:
            return state
        for row in ws.get_all_values()[1:]:
            if len(row) >= 2 and row[0] in state:
                state[row[0]] = row[1]
        return state

    def set_state(self, state: dict) -> None:
        """Overwrite A1:B(n+1) in place (no clear), so the request counter is never blank."""
        ws = self._find(STATE_B)
        if ws is None:
            ws = self._sheet.add_worksheet(title=STATE_B, rows=40, cols=4)
        rows = [["key", "value"]] + [[k, "" if state.get(k) is None else state.get(k, "")] for k in STATE_KEYS]
        ws.update(range_name="A1:B%d" % len(rows), values=rows, value_input_option="RAW")

    def write_table(self, tab: str, rows: Sequence[Sequence[object]]) -> None:
        ws = self._find(tab)
        if ws is None:
            ws = self._sheet.add_worksheet(title=tab, rows=60, cols=14)
        payload = [["" if v is None else sheets.neutralise(v) for v in row] for row in rows]
        ws.clear()
        ws.update(range_name="A1", values=payload)


# --------------------------------------------------------------- selection ----

def run_dt(row: dict) -> Optional[datetime]:
    return outcomes.parse_utc(str(row.get("run_utc", "")))


def race_candidates(main_rows: Sequence[dict], start: Optional[datetime]) -> List[dict]:
    """Main rows under rules v2 at or after the feed B start, oldest first."""
    out = []
    for row in main_rows:
        when = run_dt(row)
        if when is None or not str(row.get("rules_version", "")).strip().startswith(RACE_VERSION_PREFIX):
            continue
        if start is not None and when < start:
            continue
        out.append(row)
    out.sort(key=run_dt)
    return out


def pick_target(main_rows: Sequence[dict], b_rows: Sequence[dict],
                start: Optional[datetime]) -> Optional[dict]:
    """The latest main row, if signals_b has no row for its run_utc; otherwise None.

    Older unmatched rows are deliberately never returned: their news cannot be
    rebuilt cheaply, so they stay unmatched (no backfill).
    """
    candidates = race_candidates(main_rows, start)
    if not candidates:
        return None
    latest = candidates[-1]
    matched = {run_dt(r) for r in b_rows}
    return None if run_dt(latest) in matched else latest


def window_for(target_dt: datetime, b_rows: Sequence[dict]) -> int:
    """The live rule, measured from the previous feed B row (first run: 90)."""
    previous = [run_dt(r) for r in b_rows if run_dt(r) is not None]
    gap = (target_dt - max(previous)).total_seconds() / 60.0 if previous else None
    return live.news_window_minutes(gap)


def position_for(state: dict, b_rows: Sequence[dict]):
    """(position, note). The saved state, unless it disagrees with the rows, then the rows."""
    last_row = max((run_dt(r) for r in b_rows if run_dt(r) is not None), default=None)
    saved = outcomes.parse_utc(str(state.get("last_main_run_utc", "")))
    if saved == last_row:
        return live.state_to_position(state), ""
    from scripts import replay, leaderboard
    race = leaderboard.race_rows(list(b_rows))
    position = replay.replay_variant(race)[1] if race else FLAT
    return position, "state_rebuilt_from_rows"


def position_state(position, target_run: str, now_utc: datetime) -> dict:
    out = live.position_to_state(position, now_utc)
    out.pop("last_run_utc", None)
    out["last_main_run_utc"] = target_run
    out["last_run_utc"] = now_utc.isoformat()
    return out


# --------------------------------------------------------------------- run ----

def _to_str(value) -> str:
    return "" if value is None else str(value)


def run_once(store, now_utc: Optional[datetime] = None, session=None,
             score_fn=scorer.score_news, poster=news_b.post) -> str:
    """One feed B run. Returns a short status word. Raises only on programming errors."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if not news_b.SOURCE_FILTER_LOCKED:
        news_b.say("feed B: source filter not locked yet, nothing done")
        return "filter_not_locked"

    main_rows = store.main_rows()
    b_rows = store.rows(SIGNALS_B)
    state = store.get_state()
    start = outcomes.parse_utc(str(state.get("start_utc", "")))

    target = pick_target(main_rows, b_rows, start)
    if target is None:
        news_b.say("feed B: no unmatched main row, nothing to do (nothing spent)")
        return "no_work"
    target_dt = run_dt(target)
    age = (now_utc - target_dt).total_seconds() / 60.0
    if age > MAX_ROW_AGE_MINUTES:
        news_b.say("feed B: latest main row %s is %.0f minutes old, left unmatched (nothing spent)"
                   % (target["run_utc"], age))
        return "stale"
    news_b.say("feed B: matched main row %s" % target["run_utc"])

    store.ensure_tabs()
    if start is None:
        state["start_utc"] = target["run_utc"]
        store.set_state(state)
        news_b.say("feed B: first run, start time set to %s" % target["run_utc"])

    position, repair_note = position_for(state, b_rows)
    minutes = window_for(target_dt, b_rows)
    win_start = target_dt - timedelta(minutes=minutes)
    notes: List[str] = ["feed=B newsapi.ai", "news_window_min=%d" % minutes]
    if repair_note:
        notes.append(repair_note)
    notes.append("filter=%s" % (("p%d" % news_b.SOURCE_RANK_END_PERCENTILE)
                                if news_b.SOURCE_RANK_END_PERCENTILE is not None else "none"))

    # ---- news (budgeted, persisted before every request)
    def persist(n: int) -> None:
        state["requests_used"] = str(n)
        store.set_state(state)

    used = int(float(state.get("requests_used") or 0))
    budget = news_b.Budget(used, persist)
    key = news_b.get_key()
    fetch = news_b.FetchResult()
    if not key:
        fetch.error = "newsapi_no_key"
    else:
        fetch = news_b.fetch_window(win_start, target_dt, key, budget, session=session,
                                    end_percentile=news_b.SOURCE_RANK_END_PERCENTILE, poster=poster)
    state["requests_used"] = str(budget.used)
    notes.append("newsapi_requests=%d lifetime=%d" % (fetch.requests, budget.used))
    if fetch.cut_short:
        notes.append("window_cut_short_by_%d_request_limit" % news_b.MAX_REQUESTS_PER_RUN
                     if not fetch.cap_reached else "window_cut_short_lifetime_cap_reached")
    if fetch.error:
        notes.append(fetch.error)
    api_failed = bool(fetch.error)

    fresh: List = []
    if not api_failed:
        known = store.known_hashes(config.DEDUPE_LOOKBACK_ROWS)
        fresh = news_b.window_headlines(fetch.articles, win_start, target_dt, known, minutes)
    news_b.say("feed B: window %d min, requests %d, new headlines %d%s"
               % (minutes, fetch.requests, len(fresh), ", API FAILED" if api_failed else ""))

    # ---- score, with feed B's own history as context
    recent = b_rows[-config.CONTEXT_SIGNAL_ROWS:]
    scored = None
    if fresh:
        scored, usage = score_fn([h.as_dict() for h in fresh], recent, window_minutes=minutes)
        if usage:
            notes.append(usage.as_note())
        if scored is None:
            notes.append(usage.error or "scorer_unavailable")
    elif not api_failed:
        notes.append("no_new_news")

    if scored is not None:
        fields = {"score": scored.score, "confidence": scored.confidence, "direction": scored.direction,
                  "key_headlines": " | ".join(scored.key_headlines),
                  "new_information": scored.new_information, "reasoning": scored.reasoning}
    elif api_failed or fresh:
        fields = {"score": "", "confidence": "", "direction": "", "key_headlines": "",
                  "new_information": "",
                  "reasoning": "news fetch failed" if api_failed else "scoring failed"}
    else:
        fields = live.carry_forward(list(recent))

    # ---- rules: the same path as the live run, on feed B's own position
    price = outcomes.to_float(target.get("brent_price"))
    now_chicago = rules.to_chicago(target_dt)
    if scored is None and (fresh or api_failed):
        decision = rules.decide(score=None, confidence=None, position=position,
                                now_chicago=now_chicago, price=price)
        if decision.action != "close":
            decision = rules.Decision(action="error", position_after=decision.position_after,
                                      notes="scoring_error" if not api_failed else "news_error")
    else:
        decision = rules.decide(
            score=fields["score"] if fields["score"] != "" else None,
            confidence=fields["confidence"] or None,
            position=position, now_chicago=now_chicago, price=price)
    if decision.notes:
        notes.append(decision.notes)

    new_position = decision.position_after
    if decision.action in ("buy", "sell"):
        from .rules import Position
        new_position = Position(side=new_position.side, entry_price=new_position.entry_price,
                                entry_utc=target["run_utc"], entry_score=new_position.entry_score,
                                best_price=new_position.best_price,
                                trade_id=live.make_trade_id(new_position.side, target_dt))

    row = {
        "run_utc": target["run_utc"],
        "run_chicago": target.get("run_chicago", ""),
        "rules_version": ROW_VERSION,
        "market_open": target.get("market_open", ""),
        "brent_price": target.get("brent_price", ""),
        "price_time_utc": target.get("price_time_utc", ""),
        "new_headline_count": len(fresh),
        "action": decision.action,
        "position_after": new_position.side,
        "notes": news_b.redact("; ".join(n for n in notes if n)),
        "price_1h": "", "price_4h": "", "price_24h": "", "price_72h": "",
        "move_1h_pct": "", "move_4h_pct": "", "move_24h_pct": "", "move_72h_pct": "",
        "score_source": "live" if scored is not None else ("" if (fresh or api_failed) else "carried"),
        "feed": FEED_MARK,
    }
    row.update(fields)

    # Row first, then headlines, then state: a crash between them leaves a complete,
    # matched row (state is rebuilt from the rows next run), never news that was
    # marked as seen without a row to show for it.
    store.append(SIGNALS_B, [row])
    if fresh:
        store.append(HEADLINES_B, [dict(h.as_dict(), run_utc=target["run_utc"]) for h in fresh])
    new_state = position_state(new_position, target["run_utc"], now_utc)
    new_state["start_utc"] = state.get("start_utc", "")
    new_state["requests_used"] = str(budget.used)
    store.set_state(new_state)
    news_b.say("feed B: wrote signals_b row %s action=%s position=%s score=%s"
               % (target["run_utc"], decision.action, new_position.side, _to_str(fields["score"]) or "blank"))

    # ---- standings and the paired comparison; a side table, never fatal
    try:
        from scripts import leaderboard_b
        table = leaderboard_b.build(main_rows, b_rows + [row], state_start=new_state["start_utc"])
        leaderboard_b.write_tab(store, table)
        news_b.say("feed B: leaderboard_b tab written (%d common rows)" % table["paired"]["common_rows"])
    except Exception as exc:  # noqa: BLE001
        news_b.say("feed B: leaderboard_b skipped: %s: %s" % (type(exc).__name__, exc))
    return "wrote"


def main(argv=None) -> int:
    """Exit 0 always: a failed feed B run must never turn the workflow, or anything else, red."""
    try:
        run_once(SheetStore())
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - SystemExit comes from a bad config
        news_b.say("feed B: run failed, exit 0: %s: %s" % (type(exc).__name__, str(exc)[:300]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
