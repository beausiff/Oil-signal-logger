"""Check the IG demo login and list Brent markets with their epics.

    python -m scripts.ig_check            # searches "Brent"
    python -m scripts.ig_check "Oil"

Read only. Places no orders. Copy the epic you want into the IG_EPIC secret.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from src import broker


def main(argv=None) -> int:
    term = (argv or sys.argv[1:] or ["Brent"])[0]
    ig = broker.IG()
    try:
        ig.login()
    except broker.BrokerError as exc:
        print("login failed: %s" % exc)
        return 1
    print("login ok")

    data = ig._call("GET", "/markets?searchTerm=%s" % quote(term), 1)
    markets = data.get("markets", [])
    print("%d markets for %r:" % (len(markets), term))
    for m in markets:
        print("  %-28s expiry=%-8s type=%-12s %s" % (
            m.get("epic"), m.get("expiry"), m.get("instrumentType"), m.get("instrumentName")))

    open_now = ig._call("GET", "/positions", 2).get("positions", [])
    print("open positions on this account: %d" % len(open_now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
