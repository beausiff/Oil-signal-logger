"""Google Sheets access. The sheet is the database.

Credentials come from the GOOGLE_SERVICE_ACCOUNT_JSON environment variable,
which holds the whole service account JSON as a single secret.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Optional, Sequence

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

SIGNALS = "signals"
TRADES = "trades"
HEADLINES = "headlines"
STATE = "state"
WEEKEND_GAPS = "weekend_gaps"
CONTEXT = "context"

SCHEMAS: Dict[str, List[str]] = {
    SIGNALS: [
        "run_utc", "run_chicago", "rules_version", "market_open", "brent_price",
        "price_time_utc", "new_headline_count", "score", "confidence", "direction",
        "new_information", "key_headlines", "reasoning", "action", "position_after",
        "notes", "price_1h", "price_4h", "price_24h", "price_72h",
        "move_1h_pct", "move_4h_pct", "move_24h_pct", "move_72h_pct",
        # Appended, never inserted: every column above keeps its position, so
        # rows written before this existed still read correctly.
        "score_source",
        # Appended after score_source, same rule. Fail soft context columns
        # (src/context.py): "c1" on every row the new code writes, then three
        # values that stay blank whenever their data is unavailable.
        "context_version", "vol20_pct", "ma200", "event_flags",
    ],
    TRADES: [
        "trade_id", "side", "entry_utc", "entry_price", "entry_score", "exit_utc",
        "exit_price", "exit_reason", "best_price", "pnl_usd_bbl", "pnl_pct", "hours_held",
    ],
    HEADLINES: ["run_utc", "hash", "published_utc", "source", "title", "url"],
    # entry_score is carried here so a closed trade can report the score it opened on.
    STATE: [
        "position", "entry_price", "entry_utc", "entry_score", "best_price",
        "trade_id", "last_run_utc",
    ],
    WEEKEND_GAPS: [
        "weekend_start_date", "friday_last_price", "friday_last_score",
        "friday_position_closed", "friday_exit_price", "weekend_run_count",
        "weekend_avg_score", "weekend_max_score", "weekend_min_score",
        "weekend_key_headlines", "weekend_implied_action", "sunday_reopen_price",
        "monday_12utc_price", "gap_pct", "gap_direction_matched_weekend_score",
        "held_through_pnl_pct", "held_through_pnl_monday_pct", "weekend_signal_pnl_pct",
    ],
    # One wide row per run, keyed by run_utc. Written after the signals row,
    # in its own try/except (src/main.py), so it can never affect a run.
    CONTEXT: [
        # run meta
        "run_utc", "context_version", "github_run_id", "github_run_attempt",
        "trigger", "git_sha", "rules_version", "model_id", "prompt_hash",
        "llm_input_tokens", "llm_output_tokens", "llm_latency_ms", "llm_stop_reason",
        "llm_attempts", "price_fetch_ms", "price_age_min", "run_duration_ms",
        # news meta
        "news_window_min", "news_fetched", "news_outside_window",
        "news_duplicates_dropped", "news_over_cap_dropped", "news_new_sent",
        "news_distinct_sources", "news_newest_age_min", "news_oldest_age_min",
        "news_feed_errors",
        # price path before this run, from earlier signals rows
        "prior_move_1h_pct", "prior_move_4h_pct", "prior_move_24h_pct",
        "prior_move_72h_pct", "hours_since_prev_run", "prev_score", "runs_last_24h",
        # daily context
        "daily_source", "daily_last_date", "daily_points", "daily_log_days_added",
        "ma50", "ma200", "vol20_pct", "vol60_pct", "dist_ma200_pct",
        "mom_21d_pct", "mom_63d_pct", "mom_252d_pct", "range20_pos",
        # cross market (FRED), each with its own as of date
        "wti_price", "wti_asof", "brent_wti_spread", "brent_wti_spread_asof",
        "ovx", "ovx_asof", "usd_broad", "usd_broad_asof",
        # events
        "event_flags", "hours_to_next_eia", "days_to_next_opec",
        # weekly fundamentals
        "eia_crude_stocks_kbbl", "eia_crude_stocks_chg_kbbl", "eia_crude_stocks_period",
        "cftc_mm_net", "cftc_mm_net_chg", "cftc_open_interest", "cftc_report_date",
        # failures
        "context_errors",
    ],
}

EMPTY_STATE = {
    "position": "flat",
    "entry_price": "",
    "entry_utc": "",
    "entry_score": "",
    "best_price": "",
    "trade_id": "",
    "last_run_utc": "",
}


def column_letter(index_zero_based: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    n = index_zero_based + 1
    letters = ""
    while n:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def neutralise(value):
    """Stop third party text being read as a formula.

    Values are written with USER_ENTERED, so a headline or a model's reasoning
    that starts with = + - or @ would be evaluated by Sheets. A leading
    apostrophe makes it plain text. Numbers, bools and numeric strings stay as
    they are.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        try:
            float(value)
        except ValueError:
            return "'" + value
    return value


def _row_from_dict(tab: str, values: dict) -> list:
    return [
        "" if values.get(col) is None else neutralise(values.get(col, ""))
        for col in SCHEMAS[tab]
    ]


def _fit_columns(ws, count: int) -> None:
    """Grow a tab's grid to at least `count` columns.

    A tab made when the schema was shorter has no cells for a new column, and
    Sheets refuses a write past the grid. Never shrinks.
    """
    if getattr(ws, "col_count", count) < count:
        ws.resize(cols=count)


def header_extension(current: List[str], wanted: List[str]) -> Optional[List[str]]:
    """The header cells to add when `current` is a strict prefix of `wanted`.

    An empty row gets the whole header. None means nothing to do or nothing
    safe to do: an identical header, or one that differs anywhere but the end.
    In the last case the columns are not ours to rearrange, so they are left
    alone.
    """
    trimmed = list(current)
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    if not trimmed:
        return list(wanted)
    if len(trimmed) >= len(wanted):
        return None
    if trimmed != wanted[: len(trimmed)]:
        return None
    return wanted[len(trimmed):]


class SheetsClient:
    """Thin gspread wrapper. One sheet, five tabs."""

    def __init__(self, sheet_id: Optional[str] = None):
        import gspread
        from google.oauth2.service_account import Credentials

        sheet_id = sheet_id or os.environ.get("SHEET_ID", "").strip()
        if not sheet_id:
            raise RuntimeError("SHEET_ID is not set")

        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")

        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        self._sheet = self._gc.open_by_key(sheet_id)
        self._tabs: Dict[str, object] = {}

    # ---------------------------------------------------------------- tabs --
    def worksheet(self, tab: str):
        if tab not in self._tabs:
            self._tabs[tab] = self._sheet.worksheet(tab)
        return self._tabs[tab]

    def ensure_tabs(self) -> List[str]:
        """Create any missing tab and write its header row. Returns what changed."""
        existing = {ws.title: ws for ws in self._sheet.worksheets()}
        changed = []
        for tab, header in SCHEMAS.items():
            ws = existing.get(tab)
            if ws is None:
                ws = self._sheet.add_worksheet(title=tab, rows=2000, cols=max(len(header), 10))
                changed.append("created %s" % tab)
            current = ws.row_values(1)
            if current != header:
                _fit_columns(ws, len(header))
                ws.update(
                    range_name="A1:%s1" % column_letter(len(header) - 1),
                    values=[header],
                )
                changed.append("header written on %s" % tab)
            self._tabs[tab] = ws

        placeholder = existing.get("Sheet1")
        if placeholder is not None and len(existing) > 1:
            try:
                self._sheet.del_worksheet(placeholder)
                changed.append("removed Sheet1")
            except Exception:  # noqa: BLE001 - cosmetic only
                pass
        return changed

    def ensure_columns(self, tab: str) -> List[str]:
        """The cheap per run guard: make `tab` carry every column of its schema.

        Creates the tab if it is missing, and appends only the missing header
        cells when the sheet's header is an older, shorter version of the
        schema (the way `score_source` once was). Never rewrites or reorders an
        existing header cell. Returns what changed. Callers wrap it in a
        try/except: it must never stop a run.
        """
        import gspread

        header = SCHEMAS[tab]
        try:
            ws = self.worksheet(tab)
        except gspread.WorksheetNotFound:
            ws = self._sheet.add_worksheet(title=tab, rows=2000, cols=max(len(header), 10))
            self._tabs[tab] = ws
            ws.update(range_name="A1:%s1" % column_letter(len(header) - 1), values=[header])
            return ["created %s" % tab]

        missing = header_extension(ws.row_values(1), header)
        if not missing:
            return []
        _fit_columns(ws, len(header))
        start = len(header) - len(missing)
        ws.update(
            range_name="%s1:%s1" % (column_letter(start), column_letter(len(header) - 1)),
            values=[missing],
        )
        return ["header extended on %s: %s" % (tab, ", ".join(missing))]

    # ---------------------------------------------------------------- read --
    def all_values(self, tab: str) -> List[List[str]]:
        return self.worksheet(tab).get_all_values()

    def records(self, tab: str) -> List[dict]:
        rows = self.all_values(tab)
        if len(rows) < 2:
            return []
        header = rows[0]
        out = []
        for offset, row in enumerate(rows[1:], start=2):
            record = {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
            record["_row"] = offset
            out.append(record)
        return out

    def last_records(self, tab: str, count: int) -> List[dict]:
        return self.records(tab)[-count:]

    def recent_hashes(self, count: int) -> List[str]:
        column = SCHEMAS[HEADLINES].index("hash") + 1
        values = self.worksheet(HEADLINES).col_values(column)[1:]
        return values[-count:]

    def get_state(self) -> dict:
        rows = self.all_values(STATE)
        if len(rows) < 2 or not any(rows[1]):
            return dict(EMPTY_STATE)
        header = rows[0]
        row = rows[1]
        return {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}

    # --------------------------------------------------------------- write --
    def append(self, tab: str, rows: Sequence[dict]) -> None:
        if not rows:
            return
        payload = [_row_from_dict(tab, row) for row in rows]
        self.worksheet(tab).append_rows(
            payload, value_input_option="USER_ENTERED", table_range="A1"
        )

    def write_state(self, state: dict) -> None:
        header = SCHEMAS[STATE]
        row = [state.get(col, "") for col in header]
        self.worksheet(STATE).update(
            range_name="A2:%s2" % column_letter(len(header) - 1), values=[row]
        )

    def update_cells(self, tab: str, row_number: int, values: Dict[str, object]) -> None:
        """Patch named columns on one row in a single batch call."""
        header = SCHEMAS[tab]
        requests = []
        for column, value in values.items():
            if column not in header:
                continue
            cell = "%s%d" % (column_letter(header.index(column)), row_number)
            requests.append({"range": cell, "values": [[neutralise(value)]]})
        if requests:
            self.worksheet(tab).batch_update(requests, value_input_option="USER_ENTERED")

    def write_table(self, tab: str, rows: Sequence[Sequence[object]]) -> None:
        """Overwrite a whole tab from A1, creating it (30 x 12) when missing."""
        import gspread

        try:
            ws = self.worksheet(tab)
        except gspread.WorksheetNotFound:
            ws = self._sheet.add_worksheet(title=tab, rows=30, cols=12)
            self._tabs[tab] = ws
        payload = [["" if v is None else neutralise(v) for v in row] for row in rows]
        ws.clear()
        ws.update(range_name="A1", values=payload)

    def find_row(self, tab: str, column: str, value: str) -> Optional[dict]:
        for record in self.records(tab):
            if str(record.get(column, "")).strip() == str(value).strip():
                return record
        return None


class DryRunSheets:
    """Prints what would be written. Reads fall back to a live client when
    credentials exist, so a dry run still sees real history."""

    def __init__(self, live: Optional[SheetsClient] = None):
        self._live = live

    def ensure_tabs(self) -> List[str]:
        print("[dry-run] would create/verify tabs: %s" % ", ".join(SCHEMAS))
        return []

    def records(self, tab: str) -> List[dict]:
        return self._live.records(tab) if self._live else []

    def last_records(self, tab: str, count: int) -> List[dict]:
        return self._live.last_records(tab, count) if self._live else []

    def recent_hashes(self, count: int) -> List[str]:
        return self._live.recent_hashes(count) if self._live else []

    def get_state(self) -> dict:
        return self._live.get_state() if self._live else dict(EMPTY_STATE)

    def append(self, tab: str, rows: Sequence[dict]) -> None:
        for row in rows:
            print("[dry-run] append %s: %s" % (tab, json.dumps(row, default=str)))

    def write_state(self, state: dict) -> None:
        print("[dry-run] state: %s" % json.dumps(state, default=str))

    def write_table(self, tab: str, rows: Sequence[Sequence[object]]) -> None:
        print("[dry-run] write table %s: %d rows" % (tab, len(rows)))

    def update_cells(self, tab: str, row_number: int, values: Dict[str, object]) -> None:
        print("[dry-run] update %s row %d: %s" % (tab, row_number, json.dumps(values, default=str)))

    def find_row(self, tab: str, column: str, value: str) -> Optional[dict]:
        return self._live.find_row(tab, column, value) if self._live else None


def open_client(dry_run: bool = False):
    if not dry_run:
        return SheetsClient()
    try:
        return DryRunSheets(SheetsClient())
    except Exception as exc:  # noqa: BLE001 - dry run must work without creds
        print("[dry-run] no live sheet access (%s), reads return empty" % exc)
        return DryRunSheets(None)
