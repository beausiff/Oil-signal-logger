"""Create the sheet tabs and headers if they are missing.

    python -m scripts.init_sheet

Needs SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON in the environment. Safe to run
more than once: it only adds what is not already there.
"""

import sys

from src.sheets import SheetsClient


def main() -> int:
    client = SheetsClient()
    changes = client.ensure_tabs()
    if changes:
        for line in changes:
            print(line)
    else:
        print("sheet already correct, nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
