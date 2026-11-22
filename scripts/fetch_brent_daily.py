"""Download daily Brent spot prices to backtest/brent_daily.csv (date,price).

    python -m scripts.fetch_brent_daily

Primary source: FRED series DCOILBRENTEU (EIA Europe Brent spot, USD/bbl).
Fallbacks, tried in order if the previous one fails or returns too little:
EIA's public RBRTEd history, then Stooq's Brent front-month future (a future,
not spot, so it is only a fallback). The source actually used is printed on a
"source:" line and also written to backtest/brent_daily_source.txt.
Standard library only.
"""

from __future__ import annotations

import csv
import io
import os
import json
import sys
import time
import urllib.error
import urllib.request

START = "2021-11-01"
OUT = os.path.join("backtest", "brent_daily.csv")
SOURCE_OUT = os.path.join("backtest", "brent_daily_source.txt")
MIN_ROWS = 1000

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU&cosd=" + START
STOOQ_URL = "https://stooq.com/q/d/l/?s=lcox.f&i=d&d1=20211101"


def http_get(url: str, tries: int = 3, timeout: int = 120) -> str:
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research script)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            last = exc
        except OSError as exc:  # timeouts, resets
            last = exc
        print("  attempt %d failed for %s: %s" % (attempt + 1, url.split("?")[0], type(last).__name__))
        time.sleep(5)
    raise last


def parse_two_columns(text: str, date_col: int = 0, price_col: int = 1):
    """Rows of (date, price) from CSV text; header and missing values ('.', '') dropped."""
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) <= max(date_col, price_col):
            continue
        d, p = rec[date_col].strip(), rec[price_col].strip()
        if len(d) != 10 or d[4] != "-" or d[7] != "-":
            continue  # header or junk
        try:
            price = float(p)
        except ValueError:
            continue  # "." or blank = missing
        rows.append((d, price))
    return rows


def from_fred():
    return parse_two_columns(http_get(FRED_URL))


def from_stooq():
    # Stooq columns: Date,Open,High,Low,Close,Volume -> use Close.
    return parse_two_columns(http_get(STOOQ_URL), 0, 4)


def from_eia():
    # EIA open data API v2 with the public DEMO_KEY (keyless in practice):
    # the same RBRTE series that FRED republishes.
    url = ("https://api.eia.gov/v2/petroleum/pri/spt/data/?api_key=DEMO_KEY"
           "&frequency=daily&data[0]=value&facets[series][]=RBRTE&start=" + START +
           "&sort[0][column]=period&sort[0][direction]=asc&length=5000")
    body = json.loads(http_get(url))
    rows = []
    for rec in body["response"]["data"]:
        try:
            rows.append((rec["period"], float(rec["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


SOURCES = [
    ("FRED DCOILBRENTEU (EIA Brent spot)", from_fred),
    ("EIA API v2 RBRTE (Brent spot, DEMO_KEY)", from_eia),
    ("Stooq LCO.F (Brent front-month future, not spot)", from_stooq),
]


def main() -> int:
    for name, fn in SOURCES:
        try:
            rows = fn()
        except Exception as exc:  # network, HTTP, parse: try the next source
            print("%s failed: %s: %s" % (name, type(exc).__name__, str(exc)[:120]))
            continue
        rows = sorted(set(r for r in rows if r[0] >= START))
        print("%s: %d rows" % (name, len(rows)))
        if len(rows) < MIN_ROWS:
            print("too few rows, trying next source")
            continue
        os.makedirs("backtest", exist_ok=True)
        with open(OUT, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "price"])
            w.writerows(rows)
        with open(SOURCE_OUT, "w") as fh:
            fh.write("%s\nrows=%d first=%s last=%s\n" % (name, len(rows), rows[0][0], rows[-1][0]))
        print("source: %s" % name)
        print("wrote %s: %d rows, %s to %s" % (OUT, len(rows), rows[0][0], rows[-1][0]))
        return 0
    print("all sources failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
