"""Export every tab of the sheet to CSV in the repository.

    python -m scripts.backup

The sheet is the only copy of this experiment's data. The code is in git with
full history; the rows it was all built to collect are in one Google
document that anyone with access can delete. This makes them as durable as the
code, for nothing.

Deterministic on purpose: same data in, byte identical file out, so a week
with no new rows produces no commit.
"""

from __future__ import annotations

import csv
import os
import sys

from src import sheets
from src.sheets import SCHEMAS

OUT_DIR = "backup"
# Feed B tabs (src/feed_b.py). Not in sheets.SCHEMAS on purpose; a tab that does
# not exist yet is skipped by the loop below, like any other.
FEED_B_TABS = ["signals_b", "headlines_b", "leaderboard_b", "feed_b_state"]


def main() -> int:
    client = sheets.open_client(dry_run=False)
    os.makedirs(OUT_DIR, exist_ok=True)
    total = 0

    for tab in sorted(SCHEMAS) + FEED_B_TABS:
        try:
            values = client.all_values(tab)
        except Exception as error:  # noqa: BLE001 - a missing tab must not lose the others
            print("%-14s SKIPPED: %s" % (tab, error))
            continue
        path = os.path.join(OUT_DIR, "%s.csv" % tab)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle, lineterminator="\n").writerows(values)
        rows = max(0, len(values) - 1)
        total += rows
        print("%-14s %5d rows -> %s" % (tab, rows, path))

    print("total data rows backed up: %d" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
