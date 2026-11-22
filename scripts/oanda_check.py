"""Check the OANDA practice login and list matching markets.

    python -m scripts.oanda_check            # searches "Brent"
    python -m scripts.oanda_check "Oil"

Read only. Places no orders. Copy the name you want into the OANDA_INSTRUMENT
variable (the default, BCO_USD, is Brent).
"""

from __future__ import annotations

import sys

from src import broker


def main(argv=None) -> int:
    term = (argv or sys.argv[1:] or ["Brent"])[0].lower()
    client = broker.Oanda()
    try:
        client.login()
    except broker.BrokerError as exc:
        print("login failed: %s" % exc)
        return 1
    print("login ok")

    found = [i for i in client.instruments()
             if term in (i.get("name", "") + " " + i.get("displayName", "")).lower()]
    print("%d markets for %r:" % (len(found), term))
    for i in found:
        print("  %-16s type=%-8s %s" % (i.get("name"), i.get("type"), i.get("displayName")))

    open_now = client._call("GET", "/openTrades").get("trades", [])
    print("open trades on this account: %d" % len(open_now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
