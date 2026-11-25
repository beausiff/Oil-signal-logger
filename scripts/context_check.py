"""Fetch every external context feed once and say whether it worked.

    python -m scripts.context_check

Run it on a GitHub runner with the `context_check` workflow: the EIA and CFTC
feeds are blocked from some machines and open from there. It reads public data
only, never touches the sheet, and prints no secrets. Exit code 1 when any
feed failed, so the workflow run goes red.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from src import context


def describe(name: str, payload) -> str:
    """One line with the parsed value, so a human can eyeball it against the source."""
    if name.startswith("fred_"):
        day, value = payload[-1]
        return "%d points, latest %s on %s" % (len(payload), value, day)
    if name == "eia_stocks":
        return "crude stocks %s kbbl, week change %s, week ending %s" % (
            payload["level"], payload["change"], payload["period"])
    if name == "cftc_cot":
        return "managed money net %s, change %s, open interest %s, report date %s" % (
            payload["net"], payload["change"], payload["open_interest"], payload["report_date"])
    return str(payload)


def main() -> int:
    now = datetime.now(timezone.utc)
    print("context check at %s (feed timeout %ds each, one try)"
          % (now.isoformat(), context.FETCH_TIMEOUT_SECONDS))
    feeds = context.fetch_all(now)

    failed = 0
    for name in sorted(feeds):
        result = feeds[name]
        if result.ok:
            print("%-12s OK      %s" % (name, describe(name, result.payload)))
        else:
            failed += 1
            print("%-12s FAILED  %s" % (name, result.error))

    # Local inputs, no network.
    opec = context.load_opec_dates()
    print("%-12s %s  %d meeting date(s): %s" % (
        "opec_file", "OK    " if opec else "EMPTY ", len(opec),
        ", ".join(d.isoformat() for d in opec) or "none"))
    print("%-12s OK      flags=%r hours_to_next_eia=%s days_to_next_opec=%s" % (
        "events", context.event_flags(now, opec),
        context.hours_to_next_eia(now), context.days_to_next_opec(now, opec)))
    print("%-12s %s  %s" % (
        "daily_file", "OK    " if context.load_daily_file() else "FAILED",
        "%d fallback points" % len(context.load_daily_file())))
    print("%-12s OK      %s" % ("prompt_hash", context.prompt_hash() or "(blank)"))

    print("%d of %d network feeds failed" % (failed, len(feeds)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
