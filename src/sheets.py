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

SCHEMAS: Dict[str, List[str]] = {
    SIGNALS: [
        "run_utc", "run_chicago", "rules_version", "market_open", "brent_price",
        "price_time_utc", "new_headline_count", "score", "confidence", "direction",
        "new_information", "key_headlines", "reasoning", "action", "position_after",
        "notes", "price_1h", "price_4h", "price_24h", "price_72h",
        "move_1h_pct", "move_4h_pct", "move_24h_pct", "move_72h_pct",
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


def _row_from_dict(tab: str, values: dict) -> list:
    return ["" if values.get(col) is None else values.get(col, "") for col in SCHEMAS[tab]]


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
            requests.append({"range": cell, "values": [[value]]})
        if requests:
            self.worksheet(tab).batch_update(requests, value_input_option="USER_ENTERED")

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
