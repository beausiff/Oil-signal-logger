"""An in-memory stand in for the Google Sheet, used by the tests."""

from typing import Dict, List, Optional

from src import sheets


class FakeSheets:
    def __init__(self, data: Optional[Dict[str, List[dict]]] = None):
        self.data: Dict[str, List[dict]] = {tab: [] for tab in sheets.SCHEMAS}
        for tab, rows in (data or {}).items():
            self.data[tab] = [dict(row) for row in rows]
            for index, row in enumerate(self.data[tab], start=2):
                row.setdefault("_row", index)
        self.state = dict(sheets.EMPTY_STATE)

    def ensure_tabs(self):
        return []

    def records(self, tab):
        return [dict(row) for row in self.data[tab]]

    def last_records(self, tab, count):
        return self.records(tab)[-count:]

    def recent_hashes(self, count):
        return [row.get("hash", "") for row in self.data[sheets.HEADLINES]][-count:]

    def get_state(self):
        return dict(self.state)

    def append(self, tab, rows):
        for row in rows:
            record = dict(row)
            record["_row"] = len(self.data[tab]) + 2
            self.data[tab].append(record)

    def write_state(self, state):
        self.state = dict(state)

    def update_cells(self, tab, row_number, values):
        for row in self.data[tab]:
            if row.get("_row") == row_number:
                row.update(values)
                return
        raise AssertionError("no row %d in %s" % (row_number, tab))

    def find_row(self, tab, column, value):
        for row in self.data[tab]:
            if str(row.get(column, "")).strip() == str(value).strip():
                return dict(row)
        return None
