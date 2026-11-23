"""Pilot: can Google News RSS itself serve HISTORICAL headlines (day level date
operators), so a backtest reads the same source as the live bot?

    python -m scripts.pilot_google_vs_live --limit 3     # smoke test
    python -m scripts.pilot_google_vs_live               # the full sample

Part A (score comparison): for the same sample as the GDELT pilot
(scripts/pilot_archive_vs_live.py), rebuild the 90 minute news window ending at
each logged run from Google News RSS (the live query plus
` after:D0 before:D1`), keep items whose pubDate falls in the window, apply the
live path's dedupe and "40 newest" step, score with src.scorer.score_news using
the same context rows, and compare with the live score.

Part B (depth check, no scoring, no model spend): for 8 weekdays between
2026-03-02 and 2026-09-15 fetch the dated query for that single day and report
how many items come back and whether pubDate matches the requested day.

HARD SPEND CAP: MAX_CALLS model requests in total across every run, counted in
backtest/pilot_google_calls_used.txt and enforced by a wrapper around the
client, so the 61st request is refused in code.

`before:` is treated as EXCLUSIVE, so a single day D is queried as
`after:D before:D+1`. The first run showed a day feed spans D 07:00Z to D+1 07:00Z
(08:00Z in winter), i.e. the operators use US Pacific days, so the day ranges are
computed in America/Los_Angeles, not UTC.

VERDICT RULE (fixed before running):
  GOOD ENOUGH:      Pearson r >= 0.60 AND share of pairs within 2 points >= 70%
  NOT GOOD ENOUGH:  r < 0.40
  BORDERLINE:       anything else
"""

from __future__ import annotations

import argparse
import calendar
import csv
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from src import config, news, stats
from scripts.pilot_archive_vs_live import (  # reused helpers
    END, MAX_ROWS, START, CountingClient, SpendCap, SpendCapExceeded,  # noqa: F401
    eligible, evenly, jaccard, title_overlap, RETEST,
)

MAX_CALLS = 60
WINDOW_MINUTES = 90
KEEP_HEADLINES = config.MAX_HEADLINES_PER_RUN  # 40
FEED_CAP = 100                 # Google News RSS returns at most 100 items
SPACING = 3.0                  # seconds between Google requests
RETRY_PAUSES = (5, 15, 45)     # 3 retries with backoff
MAX_RUNTIME_MINUTES = 50
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
DEPTH_DATES_RANGE = (date(2026, 3, 2), date(2026, 9, 15))
DEPTH_N = 8

CSV_PATH = os.path.join("backtest", "pilot_google_vs_live.csv")
TXT_PATH = os.path.join("backtest", "pilot_google_vs_live.txt")
CALLS_PATH = os.path.join("backtest", "pilot_google_calls_used.txt")
FIELDS = ["run_utc", "status", "live_score", "google_score", "live_confidence",
          "google_confidence", "live_headlines", "google_headlines", "title_overlap",
          "day_raw_max", "cost_usd", "top_live_titles", "top_google_titles"]


# ------------------------------------------------------------ pure helpers --

def verdict(r: Optional[float], within2: float, n: int) -> str:
    """The rule, fixed in advance. Only an undefined r (or no pairs) gives no verdict."""
    if r is None or n < 1:
        return "NO VERDICT (no pairs or undefined r)"
    if r >= 0.60 and within2 >= 0.70:
        return "GOOD ENOUGH"
    if r < 0.40:
        return "NOT GOOD ENOUGH"
    return "BORDERLINE"


def base_query() -> str:
    """The live query (src.news.build_query) without its `when:1d` suffix.

    `when:` is a relative window ("last day") and cannot be combined with an
    absolute after/before range, so it is the one part that has to go."""
    q = news.build_query()
    suffix = " when:1d"
    return q[: -len(suffix)] if q.endswith(suffix) else q


def dated_query(day: date) -> str:
    """One calendar day. `before:` is exclusive, so the upper bound is day + 1."""
    return "%s after:%s before:%s" % (base_query(), day.isoformat(), (day + timedelta(days=1)).isoformat())


def dated_url(day: date) -> str:
    params = dict(config.GOOGLE_NEWS_PARAMS)
    params["q"] = dated_query(day)
    return "%s?%s" % (config.GOOGLE_NEWS_RSS, urlencode(params))


# Measured on the first run: a dated single-day feed for D holds pubDates from
# D 07:00Z (08:00Z in winter) to D+1 07:00Z, so Google reads after:/before: as
# US Pacific calendar days (the feed is gl=US). Days are therefore Pacific days.
DAY_TZ = ZoneInfo("America/Los_Angeles")


def window_days(start: datetime, end: datetime) -> List[date]:
    """Google (Pacific) calendar days a window touches: one, or two when it crosses Pacific midnight."""
    d0, d1 = start.astimezone(DAY_TZ).date(), end.astimezone(DAY_TZ).date()
    out, d = [], d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def parse_feed(content: bytes) -> Tuple[List[news.Headline], int]:
    """(headlines, raw item count). Reuses the live path's title/source/hash helpers."""
    import feedparser
    feed = feedparser.parse(content)
    out: List[news.Headline] = []
    for entry in feed.entries:
        parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if not parsed:
            continue
        published = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
        title = news._clean_title(getattr(entry, "title", ""))
        if not title:
            continue
        source = news._entry_source(entry)
        out.append(news.Headline(hash=news.headline_hash(title, source), published_utc=published.isoformat(),
                                 source=source, title=title, url=getattr(entry, "link", "") or ""))
    return out, len(feed.entries)


def slice_window(headlines: Sequence[news.Headline], start: datetime, end: datetime) -> List[news.Headline]:
    """Headlines whose pubDate lies in [start, end]."""
    out = []
    for h in headlines:
        p = news._parse_iso(h.published_utc)
        if p is not None and start <= p <= end:
            out.append(h)
    return out


def window_headlines(day_feeds: Sequence[Sequence[news.Headline]], run_dt: datetime) -> List[news.Headline]:
    """Merge the day feeds, slice to the 90 minute window, then the live path's
    dedupe and 40-newest step."""
    start = run_dt - timedelta(minutes=WINDOW_MINUTES)
    merged = [h for feed in day_feeds for h in feed]
    inside = slice_window(merged, start, run_dt)
    return news.filter_and_dedupe(inside, [], run_dt, lookback_minutes=WINDOW_MINUTES, cap=KEEP_HEADLINES)


def sample_rows(signals: Sequence[dict]) -> List[dict]:
    """Same sample as the GDELT pilot: up to 56 eligible rows, evenly spaced."""
    return evenly([r for r in signals if eligible(r)], MAX_ROWS)


def depth_dates() -> List[date]:
    """8 weekdays spread evenly across the range (weekend picks move to the nearest weekday inside it)."""
    lo, hi = DEPTH_DATES_RANGE
    span = (hi - lo).days
    out = []
    for i in range(DEPTH_N):
        d = lo + timedelta(days=round(i * span / (DEPTH_N - 1)))
        if d.weekday() == 5:
            d -= timedelta(days=1)
        elif d.weekday() == 6:
            d += timedelta(days=1)
        d = min(max(d, lo), hi)
        out.append(d)
    return out


def depth_stats(day: date, headlines: Sequence[news.Headline], raw_count: int) -> dict:
    pubs = sorted(h.published_utc for h in headlines)
    on_day = sum(1 for h in headlines if h.published_utc[:10] == day.isoformat())
    on_day_pt = sum(1 for h in headlines
                    if datetime.fromisoformat(h.published_utc).astimezone(DAY_TZ).date() == day)
    return {"date": day.isoformat(), "items": raw_count, "parsed": len(headlines), "on_day": on_day,
            "on_day_pt": on_day_pt,
            "earliest": pubs[0] if pubs else "", "latest": pubs[-1] if pubs else "",
            "samples": [(h.title, h.source) for h in headlines[:3]]}


# ----------------------------------------------------------------- fetch ----

_last_request = [0.0]


def fetch_day(day: date, session=None, sleep=time.sleep, clock=time.time) -> Optional[Tuple[List[news.Headline], int]]:
    """Fetch one dated feed, or None if every attempt failed. >= SPACING between requests."""
    import requests
    session = session or requests.Session()
    url = dated_url(day)
    for attempt in range(len(RETRY_PAUSES) + 1):
        wait = SPACING - (clock() - _last_request[0])
        if wait > 0:
            sleep(wait)
        _last_request[0] = clock()
        status = None
        try:
            resp = session.get(url, timeout=config.HTTP_TIMEOUT_SECONDS,
                               headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
            status = resp.status_code
            if status == 200:
                return parse_feed(resp.content)
        except Exception as exc:  # noqa: BLE001 - network errors are retried
            status = type(exc).__name__
        if attempt < len(RETRY_PAUSES):
            print("  google %s for %s, pausing %ds" % (status, day, RETRY_PAUSES[attempt]), flush=True)
            sleep(RETRY_PAUSES[attempt])
    return None


class DayCache:
    """Each day's feed is fetched once per process."""

    def __init__(self, fetch=fetch_day):
        self._fetch, self._store = fetch, {}

    def get(self, day: date):
        if day not in self._store:
            self._store[day] = self._fetch(day)
        return self._store[day]


# ----------------------------------------------------------------- report ---

def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_report(rows: List[dict], sampled: int, calls_used: int, depth: List[dict]) -> str:
    ok = [r for r in rows if r["status"] == "ok"]
    n = len(ok)
    live = [float(r["live_score"]) for r in ok]
    goo = [float(r["google_score"]) for r in ok]
    diffs = [abs(a - b) for a, b in zip(live, goo)]
    out: List[str] = []
    w = out.append
    w("PILOT: Google News RSS with date operators versus the live feed")
    w("Rule (fixed before running): GOOD ENOUGH = Pearson r >= 0.60 AND >= 70% of pairs within 2 points; "
      "NOT GOOD ENOUGH = r < 0.40; BORDERLINE = anything else.")
    w("Context: " + RETEST + ".")
    w("Query: live terms with ` after:D before:D+1` appended (the live `when:1d` is dropped, it cannot be "
      "combined with absolute dates); `before:` treated as exclusive. Window = 90 minutes ending at run_utc. Days are US Pacific days (measured: Google reads the operators in Pacific time).")
    w("")
    w("== PART A: score comparison ==")
    w("rows sampled (rows in the sample): %d" % sampled)
    w("pairs scored: %d of %d rows sampled" % (n, sampled))
    w("rows with zero headlines in window: %d" % sum(1 for r in rows if r["status"] == "no_headlines"))
    w("rows skipped for Google fetch failure: %d" % sum(1 for r in rows if r["status"] == "google_failed"))
    w("rows not scored for model failure or spend cap: %d" % sum(1 for r in rows if r["status"] in ("model_failed", "budget")))
    w("rows not yet processed: %d" % (sampled - len(rows)))
    w("rows where a raw day feed hit %d items (possible truncation cap): %d"
      % (FEED_CAP, sum(1 for r in rows if (_f(r.get("day_raw_max")) or 0) >= FEED_CAP)))
    r = None
    if n:
        r = stats.pearson(live, goo)
        w("Pearson r (google vs live): %s" % ("n/a" if r is None else "%.3f" % r))
        if r is not None and n >= 4:
            lo, hi = stats.fisher_ci(r, n)
            w("Fisher 95%% interval for r: [%.3f, %.3f]" % (lo, hi))
        else:
            w("Fisher 95% interval for r: n/a")
        w("mean absolute difference: %.2f points" % (sum(diffs) / n))
        w("identical: %.0f%%   within 1: %.0f%%   within 2: %.0f%%" % (
            100.0 * sum(d == 0 for d in diffs) / n, 100.0 * sum(d <= 1 for d in diffs) / n,
            100.0 * sum(d <= 2 for d in diffs) / n))
        lh = [float(x["live_headlines"]) for x in ok]
        gh = [float(x["google_headlines"]) for x in ok]
        w("average headline count: live %.1f, Google dated %.1f" % (sum(lh) / n, sum(gh) / n))
        ov = [float(x["title_overlap"]) for x in ok if x.get("title_overlap") not in ("", None, "")]
        w("average title overlap with logged live headlines (share with a >=60%% Jaccard match): %s"
          % ("%.0f%%" % (100 * sum(ov) / len(ov)) if ov else "n/a"))
    w("")
    w("VERDICT: %s" % verdict(r, (sum(d <= 2 for d in diffs) / n) if n else 0.0, n))
    w("")
    spend = sum(_f(x.get("cost_usd")) or 0.0 for x in rows)
    w("total model requests counted against the cap: %d of %d   model spend (scorer usage cost): $%.4f"
      % (calls_used, MAX_CALLS, spend))
    w("")
    w("== 5 example rows (evenly spaced through the pairs) ==")
    for x in evenly(ok, 5):
        w("%s  live %+d (%s)  google %+d (%s)  overlap %s" % (
            x["run_utc"][:16], int(float(x["live_score"])), x["live_confidence"],
            int(float(x["google_score"])), x["google_confidence"], x.get("title_overlap", "")))
        w("  live:   " + (x["top_live_titles"] or "(none)"))
        w("  google: " + (x["top_google_titles"] or "(none)"))
    w("")
    w("== PART B: depth check (single day dated query, no scoring) ==")
    if not depth:
        w("(not run)")
    for d in depth:
        if d.get("failed"):
            w("%s  FETCH FAILED" % d["date"])
            continue
        w("%s  items returned: %d   pubDate on requested day: %d (UTC date) / %d (Pacific date)   earliest: %s   latest: %s" % (
            d["date"], d["items"], d["on_day"], d["on_day_pt"], d["earliest"] or "-", d["latest"] or "-"))
        for title, source in d["samples"]:
            w("    - %s  [%s]" % (title, source))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ main ----

def load_csv() -> Dict[str, dict]:
    if not os.path.exists(CSV_PATH):
        return {}
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        return {r["run_utc"]: r for r in csv.DictReader(fh)}


def save(done: Dict[str, dict], order: List[str]) -> None:
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for k in order:
            if k in done:
                w.writerow({f: done[k].get(f, "") for f in FIELDS})


def run_depth(cache: DayCache) -> List[dict]:
    out = []
    for d in depth_dates():
        res = cache.get(d)
        if res is None:
            out.append({"date": d.isoformat(), "failed": True})
            print("  depth %s fetch failed" % d, flush=True)
            continue
        s = depth_stats(d, res[0], res[1])
        out.append(s)
        print("  depth %s items %d on_day %d" % (d, s["items"], s["on_day"]), flush=True)
    return out


def run(limit: int, skip_depth: bool = False) -> int:
    from src import scorer
    with open("backup/signals.csv", newline="", encoding="utf-8") as fh:
        signals = sorted(csv.DictReader(fh), key=lambda r: r["run_utc"])
    index = {r["run_utc"]: i for i, r in enumerate(signals)}
    sample = sample_rows(signals)
    order = [r["run_utc"] for r in sample]
    live_titles: Dict[str, List[dict]] = {}
    with open("backup/headlines.csv", newline="", encoding="utf-8") as fh:
        for h in csv.DictReader(fh):
            live_titles.setdefault(h["run_utc"], []).append(h)

    done = load_csv()
    cap = SpendCap(CALLS_PATH, MAX_CALLS)
    print("sample %d rows, already done %d, calls used %d of %d" % (
        len(sample), sum(1 for v in done.values() if v["status"] in ("ok", "no_headlines")), cap.used, cap.max_calls),
        flush=True)
    cache = DayCache()
    client = None
    processed = 0
    t0 = time.time()
    for row in sample:
        key = row["run_utc"]
        if done.get(key, {}).get("status") in ("ok", "no_headlines"):
            continue
        if limit and processed >= limit:
            break
        if time.time() - t0 > MAX_RUNTIME_MINUTES * 60:
            print("runtime budget reached, stopping; re-dispatch to resume", flush=True)
            break
        processed += 1
        run_dt = datetime.fromisoformat(key)
        rec = {"run_utc": key, "status": "", "live_score": row["score"], "live_confidence": row["confidence"],
               "live_headlines": row["new_headline_count"]}
        lts = sorted(live_titles.get(key, []), key=lambda h: h.get("published_utc", ""), reverse=True)
        rec["top_live_titles"] = " | ".join(h["title"] for h in lts[:3])

        feeds, raw_max, failed = [], 0, False
        for day in window_days(run_dt - timedelta(minutes=WINDOW_MINUTES), run_dt):
            res = cache.get(day)
            if res is None:
                failed = True
                break
            feeds.append(res[0])
            raw_max = max(raw_max, res[1])
        if failed:
            rec["status"] = "google_failed"
            done[key] = rec
            save(done, order)
            print("  %s Google fetch failed, row skipped" % key[:16], flush=True)
            continue
        rec["day_raw_max"] = raw_max
        heads = window_headlines(feeds, run_dt)
        rec["google_headlines"] = len(heads)
        rec["top_google_titles"] = " | ".join(h.title for h in heads[:3])
        if not heads:
            rec["status"] = "no_headlines"
            done[key] = rec
            save(done, order)
            print("  %s zero headlines in window (day feed items %d)" % (key[:16], raw_max), flush=True)
            continue
        if lts:
            rec["title_overlap"] = "%.3f" % (title_overlap([h["title"] for h in lts], [h.title for h in heads]) or 0.0)
        if cap.remaining <= 0:
            rec["status"] = "budget"
            done[key] = rec
            save(done, order)
            continue
        if client is None:
            client = CountingClient(scorer._client(), cap)
        i = index[key]
        context = signals[max(0, i - config.CONTEXT_SIGNAL_ROWS):i]
        scored, usage = scorer.score_news([h.as_dict() for h in heads], context, client=client)
        rec["cost_usd"] = "%.6f" % usage.cost_usd
        if scored is None:
            rec["status"] = "model_failed"
            print("  %s model failed: %s" % (key[:16], usage.error[:80]), flush=True)
        else:
            rec["status"] = "ok"
            rec["google_score"] = scored.score
            rec["google_confidence"] = scored.confidence
            print("  %s live %s google %+d  (%d google headlines, %d live, day feed %d)" % (
                key[:16], row["score"], scored.score, len(heads), int(row["new_headline_count"]), raw_max), flush=True)
        done[key] = rec
        save(done, order)

    save(done, order)
    depth = [] if skip_depth else run_depth(cache)
    rows = [done[k] for k in order if k in done]
    text = build_report(rows, len(sample), cap.used, depth)
    with open(TXT_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text, flush=True)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--limit", type=int, default=0, help="process at most N new rows this run (0 = all)")
    p.add_argument("--skip-depth", action="store_true", help="skip Part B")
    a = p.parse_args(argv)
    return run(a.limit, a.skip_depth)


if __name__ == "__main__":
    sys.exit(main())
