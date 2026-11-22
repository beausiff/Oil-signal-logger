"""Probe whether an honest news-bot backtest is possible on a clean window.

    python -m scripts.probe_clean_window

Writes plain text findings to backtest/probe_clean_window.txt. Three probes:
  1. the scoring model's own answer about its training cutoff (ONE cheap call;
     the model's self report is weak evidence, not a documented source);
  2. GDELT DOC 2.0 article lists for three 6 hour windows;
  3. OilPriceAPI price history (src.price.fetch_brent_range) for the same
     windows, plus wider windows to see what resolution exists.
Needs ANTHROPIC_API_KEY and OILPRICE_API_KEY. Prints no secret.
"""

from __future__ import annotations

import csv
import io
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from src import config

OUT = os.path.join("backtest", "probe_clean_window.txt")
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
QUERY = "(oil OR Brent OR OPEC OR Hormuz) sourcelang:english"
WINDOWS = [
    ("March 2026", datetime(2026, 3, 10, 12, tzinfo=timezone.utc)),
    ("June 2026", datetime(2026, 6, 10, 12, tzinfo=timezone.utc)),
    ("early September 2026", datetime(2026, 9, 8, 12, tzinfo=timezone.utc)),
]
lines = []


def say(text=""):
    print(text)
    lines.append(text)


def self_report():
    say("== 1. Model self report (weak evidence, NOT a documented source) ==")
    say("model: %s" % config.ANTHROPIC_MODEL)
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL, max_tokens=config.ANTHROPIC_MAX_TOKENS,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content":
                       "In one or two sentences: what is your training data cutoff date (month and year), "
                       "and what is the most recent event you know about?"}],
        )
        text = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        say("answer: " + text.replace("\n", " "))
        u = resp.usage
        say("tokens_in=%s tokens_out=%s" % (u.input_tokens, u.output_tokens))
    except Exception as exc:
        say("self report failed: %s: %s" % (type(exc).__name__, str(exc)[:200]))
    say()


def gdelt_get(params):
    url = GDELT + "?" + urllib.parse.urlencode(params)
    status, body = None, ""
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-probe"})
            with urllib.request.urlopen(req, timeout=60) as r:
                status, body = r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read().decode("utf-8", errors="replace")
        except OSError as exc:
            status, body = "error:%s" % type(exc).__name__, ""
        if status == 429 and attempt < 3:
            say("  (429 rate limited, pausing 30s and retrying)")
            time.sleep(30)
            continue
        break
    return status, body


def gdelt_probe():
    say("== 2. GDELT DOC 2.0 article lists, 6 hour windows ==")
    say("query: " + QUERY)
    for label, start in WINDOWS:
        end = start + timedelta(hours=6)
        params = {"query": QUERY, "mode": "artlist", "format": "csv", "maxrecords": 75,
                  "startdatetime": start.strftime("%Y%m%d%H%M%S"),
                  "enddatetime": end.strftime("%Y%m%d%H%M%S"), "sort": "datedesc"}
        time.sleep(20)
        status, body = gdelt_get(params)
        say("-- %s: %s to %s UTC, HTTP %s" % (label, start.strftime("%Y-%m-%d %H:%M"), end.strftime("%H:%M"), status))
        if status != 200:
            say("  body start: " + body[:160].replace("\n", " "))
            continue
        recs = list(csv.reader(io.StringIO(body)))
        header = [h.strip().lower() for h in recs[0]] if recs else []
        rows = recs[1:]
        say("  csv header: %s" % ",".join(header))
        say("  articles returned: %d (max requested 75)" % len(rows))

        def col(r, *names):
            for n in names:
                if n in header and header.index(n) < len(r):
                    return r[header.index(n)]
            return ""
        for r in rows[:5]:
            say("  %s | %s | %s" % (col(r, "seendate", "date"), col(r, "domain"),
                                  col(r, "title", "urltitle")))
    say("live system search terms: " + ", ".join(config.SEARCH_TERMS))
    say()


def price_probe():
    say("== 3. OilPriceAPI Brent history (src.price.fetch_brent_range) ==")
    from src.price import fetch_brent_range
    probes = []
    for label, start in WINDOWS:
        probes.append(("%s 6h" % label, start, start + timedelta(hours=6)))
    for label, start in WINDOWS:
        probes.append(("%s 24h" % label, start, start + timedelta(hours=24)))
    # The range endpoint returns the LAST 100 points of a window, so a 6h window
    # is truncated; these 20 minute slices at each window start show real coverage.
    for label, start in WINDOWS:
        probes.append(("%s first 20 min of window" % label, start, start + timedelta(minutes=20)))
    for label, day in (("2026-02-02 (just after Jan 2026 cutoff)", datetime(2026, 2, 2, 12, tzinfo=timezone.utc)),
                       ("2025-11-03 (before cutoff)", datetime(2025, 11, 3, 12, tzinfo=timezone.utc)),
                       ("2024-06-03 (two years back)", datetime(2024, 6, 3, 12, tzinfo=timezone.utc))):
        probes.append((label + " 20 min", day, day + timedelta(minutes=20)))
    probes.append(("March 2026 7 days", WINDOWS[0][1], WINDOWS[0][1] + timedelta(days=7)))
    probes.append(("2026-10-20 6h (recent control)", datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
                   datetime(2026, 10, 20, 18, tzinfo=timezone.utc)))
    for label, a, b in probes:
        pts = fetch_brent_range(a, b)
        if pts:
            gaps = [(pts[i + 1][0] - pts[i][0]).total_seconds() / 60 for i in range(len(pts) - 1)]
            med = sorted(gaps)[len(gaps) // 2] if gaps else 0
            say("-- %s: %d points, first %s %.2f, last %s %.2f, median gap %.0f min"
                % (label, len(pts), pts[0][0].isoformat(), pts[0][1], pts[-1][0].isoformat(), pts[-1][1], med))
        else:
            say("-- %s: 0 points (no data or call failed)" % label)
        time.sleep(1)
    say()


def main() -> int:
    say("PROBE: clean window backtest feasibility, run %s UTC" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    say()
    for fn in (self_report, gdelt_probe, price_probe):
        try:
            fn()
        except Exception as exc:
            say("%s crashed: %s: %s" % (fn.__name__, type(exc).__name__, str(exc)[:200]))
    os.makedirs("backtest", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
