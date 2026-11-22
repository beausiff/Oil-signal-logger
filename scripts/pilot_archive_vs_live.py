"""Pilot: are GDELT archive headlines a good enough stand in for the live feed?

    python -m scripts.pilot_archive_vs_live --limit 3     # smoke test
    python -m scripts.pilot_archive_vs_live               # the rest of the sample

For runs already logged LIVE (v1 rows, news window = 90 minutes before the
run), rebuild that window from GDELT DOC 2.0, score it with the SAME scorer
(src.scorer.score_news, same context rows as scripts/backfill_scores.py), and
compare with the live score. Writes backtest/pilot_archive_vs_live.csv (per
row) and backtest/pilot_archive_vs_live.txt (report). Resumes from the CSV.

HARD SPEND CAP: MAX_CALLS model requests in total across every run of the
pilot, counted in backtest/pilot_calls_used.txt and enforced by a wrapper
around the client, so the 61st request is refused in code.

VERDICT RULE (fixed before running):
  GOOD ENOUGH:      Pearson r >= 0.60 AND share of pairs within 2 points >= 70%
  NOT GOOD ENOUGH:  r < 0.40
  BORDERLINE:       anything else
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from src import config, stats

MAX_CALLS = 60
MAX_ROWS = 56
START, END = "2026-09-28", "2026-10-16"
KEEP_HEADLINES = 40
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_SPACING = 12.0   # the brief asks for >= 7s; GDELT from the runner is heavily throttled
RETRY_PAUSES = (30, 60, 120, 240)
MAX_RUNTIME_MINUTES = 310  # stop starting new rows after this; re-dispatch resumes from the CSV

CSV_PATH = os.path.join("backtest", "pilot_archive_vs_live.csv")
TXT_PATH = os.path.join("backtest", "pilot_archive_vs_live.txt")
CALLS_PATH = os.path.join("backtest", "pilot_calls_used.txt")
FIELDS = ["run_utc", "status", "live_score", "archive_score", "live_confidence",
          "archive_confidence", "live_headlines", "archive_headlines", "title_overlap",
          "cost_usd", "top_live_titles", "top_archive_titles"]

RETEST = "model test-retest on identical headlines (measured earlier): r = 0.976, 83% identical, 97% within one point"


# ------------------------------------------------------------ pure helpers --

def normalise_title(title: str) -> str:
    """Undo GDELT's tokenised punctuation: '3 - year' -> '3-year', ' , ' -> ', '."""
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(r"(?<=\w) - (?=[a-z0-9])", "-", t)          # hyphenated words only
    t = re.sub(r"\s+([,.;:!?%)\]])", r"\1", t)
    t = re.sub(r"([(\[$])\s+", r"\1", t)
    t = re.sub(r"\s'\s?(s|t|re|ve|ll|d|m)\b", r"'\1", t)
    t = re.sub(r"(?<=\w) - (?=\d)", "-", t)
    return t.strip()


def _term_pattern(terms: Sequence[str]):
    return re.compile(r"\b(?:%s)\b" % "|".join(re.escape(t) for t in terms), re.IGNORECASE)


def filter_titles(rows: Sequence[dict], terms: Sequence[str], keep: int = KEEP_HEADLINES) -> List[dict]:
    """Normalise, keep titles with a whole-word search term, drop exact duplicates,
    keep the `keep` newest. Each row: {"published_utc", "title", "url"}."""
    pat = _term_pattern(terms)
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: x.get("published_utc", ""), reverse=True):
        title = normalise_title(r.get("title", ""))
        if not title or not pat.search(title) or title in seen:
            continue
        seen.add(title)
        out.append({**r, "title": title})
    return out[:keep]


def _words(title: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3}


def jaccard(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 1.0 if a.strip().lower() == b.strip().lower() and a.strip() else 0.0
    return len(wa & wb) / len(wa | wb)


def title_overlap(live_titles: Sequence[str], archive_titles: Sequence[str], threshold: float = 0.6) -> Optional[float]:
    """Share of live titles matched by some archive title with Jaccard >= threshold."""
    if not live_titles:
        return None
    hit = sum(1 for lt in live_titles if any(jaccard(lt, at) >= threshold for at in archive_titles))
    return hit / len(live_titles)


def verdict(r: Optional[float], within2: float, n: int) -> str:
    if r is None or n < 10:
        return "NO VERDICT (too few pairs or undefined r)"
    if r >= 0.60 and within2 >= 0.70:
        return "GOOD ENOUGH"
    if r < 0.40:
        return "NOT GOOD ENOUGH"
    return "BORDERLINE"


def evenly(items: Sequence, n: int) -> list:
    if len(items) <= n:
        return list(items)
    if n == 1:
        return [items[0]]
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def eligible(row: dict) -> bool:
    day = str(row.get("run_utc", ""))[:10]
    src = str(row.get("score_source", ""))
    nh = str(row.get("new_headline_count", "")).strip()
    return (START <= day <= END and str(row.get("score", "")).strip() != ""
            and not src.startswith("backfilled") and src != "carried"
            and nh.isdigit() and int(nh) >= 5)


# ------------------------------------------------------------- spend cap ----

class SpendCapExceeded(RuntimeError):
    pass


class SpendCap:
    """Counts model requests in a file so the cap holds across separate runs."""

    def __init__(self, path: str = CALLS_PATH, max_calls: int = MAX_CALLS):
        self.path, self.max_calls = path, max_calls
        try:
            self.used = int(open(path).read().strip())
        except (OSError, ValueError):
            self.used = 0

    def consume(self) -> None:
        if self.used >= self.max_calls:
            raise SpendCapExceeded("spend cap of %d model calls reached" % self.max_calls)
        self.used += 1
        with open(self.path, "w") as fh:
            fh.write("%d\n" % self.used)  # written BEFORE the request is made

    @property
    def remaining(self) -> int:
        return self.max_calls - self.used


class _CountedMessages:
    def __init__(self, inner, cap: SpendCap):
        self._inner, self._cap = inner, cap

    def create(self, **kwargs):
        self._cap.consume()
        return self._inner.create(**kwargs)


class CountingClient:
    def __init__(self, inner, cap: SpendCap):
        self.messages = _CountedMessages(inner.messages, cap)


# ----------------------------------------------------------------- GDELT ----

_last_request = [0.0]


def gdelt_query() -> str:
    terms = " OR ".join('"%s"' % t if " " in t else t for t in config.SEARCH_TERMS)
    return "(%s) sourcelang:english" % terms


def fetch_gdelt(start: datetime, end: datetime) -> Optional[List[dict]]:
    """Articles in [start, end] UTC, or None if every attempt failed."""
    params = {"query": gdelt_query(), "mode": "artlist", "format": "csv", "maxrecords": 250,
              "sort": "datedesc", "startdatetime": start.strftime("%Y%m%d%H%M%S"),
              "enddatetime": end.strftime("%Y%m%d%H%M%S")}
    url = GDELT_URL + "?" + urllib.parse.urlencode(params)
    for attempt in range(len(RETRY_PAUSES) + 1):
        wait = GDELT_SPACING - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
        status, body = None, ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-pilot"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                status, body = resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
        except OSError:
            status = "network"
        if status == 200:
            return parse_gdelt_csv(body)
        if attempt < len(RETRY_PAUSES):
            print("  GDELT %s, pausing %ds" % (status, RETRY_PAUSES[attempt]))
            time.sleep(RETRY_PAUSES[attempt])
    return None


def parse_gdelt_csv(body: str) -> List[dict]:
    recs = list(csv.reader(io.StringIO(body.lstrip("﻿"))))
    if not recs:
        return []
    header = [h.strip().lower() for h in recs[0]]
    if "title" not in header:
        return []  # GDELT answers a zero result query with a plain message
    ti, di = header.index("title"), header.index("date") if "date" in header else -1
    ui = header.index("url") if "url" in header else -1
    out = []
    for r in recs[1:]:
        if len(r) <= ti:
            continue
        d = r[di].strip() if 0 <= di < len(r) else ""
        try:
            d = datetime.strptime(d, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
        out.append({"published_utc": d, "title": r[ti], "url": r[ui] if 0 <= ui < len(r) else ""})
    return out


def as_scorer_headline(h: dict) -> dict:
    host = urllib.parse.urlparse(h.get("url", "")).netloc
    return {"title": h["title"], "source": host, "published_utc": h.get("published_utc", ""),
            "url": h.get("url", ""), "hash": ""}


# ----------------------------------------------------------------- report ---

def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_report(rows: List[dict], sampled: int, calls_used: int) -> str:
    ok = [r for r in rows if r["status"] == "ok"]
    n = len(ok)
    live = [float(r["live_score"]) for r in ok]
    arc = [float(r["archive_score"]) for r in ok]
    diffs = [abs(a - b) for a, b in zip(live, arc)]
    out: List[str] = []
    w = out.append
    w("PILOT: GDELT archive headlines versus the live Google News feed")
    w("Rule (fixed before running): GOOD ENOUGH = Pearson r >= 0.60 AND >= 70% of pairs within 2 points; "
      "NOT GOOD ENOUGH = r < 0.40; BORDERLINE = anything else.")
    w("Context: " + RETEST + ". The pilot cannot beat that; it shows the loss from swapping the headline source.")
    w("")
    w("rows sampled (rows in the sample): %d" % sampled)
    w("rows skipped for GDELT failure: %d" % sum(1 for r in rows if r["status"] == "gdelt_failed"))
    w("rows with no archive headlines after filtering: %d" % sum(1 for r in rows if r["status"] == "no_archive"))
    w("rows not scored for model failure or spend cap: %d" % sum(1 for r in rows if r["status"] in ("model_failed", "budget")))
    w("rows not yet processed: %d" % (sampled - len(rows)))
    w("pairs scored: %d" % n)
    if n:
        r = stats.pearson(live, arc)
        w("Pearson r (archive vs live): %s" % ("n/a" if r is None else "%.3f" % r))
        cs = stats.circular_shift_test(live, arc) if n >= 30 else None
        w("circular shift p: %s" % ("%.4f" % cs["p"] if cs else "n/a (needs n >= 30)"))
        w("mean absolute difference: %.2f points" % (sum(diffs) / n))
        w("identical: %.0f%%   within 1: %.0f%%   within 2: %.0f%%" % (
            100.0 * sum(d == 0 for d in diffs) / n, 100.0 * sum(d <= 1 for d in diffs) / n,
            100.0 * sum(d <= 2 for d in diffs) / n))
        ml, ma = sum(live) / n, sum(arc) / n
        w("mean live score %+.2f, mean archive score %+.2f (archive minus live %+.2f: %s)" % (
            ml, ma, ma - ml, "archive more bullish" if ma - ml > 0.25 else
            "archive more bearish" if ma - ml < -0.25 else "no clear systematic shift"))
        big = [(a, b) for a, b in zip(live, arc) if abs(a) >= 4]
        same = sum(1 for a, b in big if b * a > 0 and abs(b) >= 4)
        w("live |score| >= 4: %d pairs, archive on the same side with |score| >= 4: %d" % (len(big), same))
        lh = [float(x["live_headlines"]) for x in ok]
        ah = [float(x["archive_headlines"]) for x in ok]
        w("average headlines: live %.1f, archive after filtering %.1f" % (sum(lh) / n, sum(ah) / n))
        ov = [float(x["title_overlap"]) for x in ok if x["title_overlap"] not in ("", None)]
        w("average title overlap (share of live titles with a >=60%% Jaccard archive match): %s"
          % ("%.0f%%" % (100 * sum(ov) / len(ov)) if ov else "n/a"))
        w("")
        w("VERDICT: %s" % verdict(r, sum(d <= 2 for d in diffs) / n, n))
    else:
        r = None
        w("")
        w("VERDICT: %s" % verdict(None, 0.0, 0))
    w("")
    spend = sum(_f(x["cost_usd"]) or 0.0 for x in rows)
    w("total model requests counted against the cap: %d of %d   model spend (scorer usage cost): $%.4f"
      % (calls_used, MAX_CALLS, spend))
    w("")
    w("== 5 example rows (evenly spaced through the pairs, not cherry picked) ==")
    for x in evenly(ok, 5):
        w("%s  live %+d (%s)  archive %+d (%s)  overlap %s" % (
            x["run_utc"][:16], int(float(x["live_score"])), x["live_confidence"],
            int(float(x["archive_score"])), x["archive_confidence"], x["title_overlap"]))
        w("  live:    " + (x["top_live_titles"] or "(none)"))
        w("  archive: " + (x["top_archive_titles"] or "(none)"))
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


def run(limit: int) -> int:
    from src import scorer
    with open("backup/signals.csv", newline="", encoding="utf-8") as fh:
        signals = sorted(csv.DictReader(fh), key=lambda r: r["run_utc"])
    index = {r["run_utc"]: i for i, r in enumerate(signals)}
    sample = evenly([r for r in signals if eligible(r)], MAX_ROWS)
    order = [r["run_utc"] for r in sample]
    live_titles: Dict[str, List[dict]] = {}
    with open("backup/headlines.csv", newline="", encoding="utf-8") as fh:
        for h in csv.DictReader(fh):
            live_titles.setdefault(h["run_utc"], []).append(h)

    done = load_csv()
    cap = SpendCap()
    print("sample %d rows, already done %d, calls used %d of %d" % (
        len(sample), sum(1 for v in done.values() if v["status"] in ("ok", "no_archive")), cap.used, cap.max_calls))
    client = None
    processed = 0
    t0 = time.time()
    for row in sample:
        key = row["run_utc"]
        if done.get(key, {}).get("status") in ("ok", "no_archive"):
            continue
        if limit and processed >= limit:
            break
        if time.time() - t0 > MAX_RUNTIME_MINUTES * 60:
            print("runtime budget reached, stopping; re-dispatch to resume")
            break
        processed += 1
        run_dt = datetime.fromisoformat(key)
        rec = {"run_utc": key, "status": "", "live_score": row["score"], "live_confidence": row["confidence"],
               "live_headlines": row["new_headline_count"], "archive_score": "", "archive_confidence": "",
               "archive_headlines": "", "title_overlap": "", "cost_usd": "", "top_live_titles": "",
               "top_archive_titles": ""}
        lts = sorted(live_titles.get(key, []), key=lambda h: h.get("published_utc", ""), reverse=True)
        rec["top_live_titles"] = " | ".join(h["title"] for h in lts[:3])

        raw = fetch_gdelt(run_dt - timedelta(minutes=90), run_dt)
        if raw is None:
            rec["status"] = "gdelt_failed"
            done[key] = rec
            save(done, order)
            print("  %s GDELT failed, row skipped" % key[:16])
            continue
        heads = filter_titles(raw, config.SEARCH_TERMS)
        rec["archive_headlines"] = len(heads)
        rec["top_archive_titles"] = " | ".join(h["title"] for h in heads[:3])
        if not heads:
            rec["status"] = "no_archive"
            done[key] = rec
            save(done, order)
            print("  %s no archive headlines (raw %d)" % (key[:16], len(raw)))
            continue
        rec["title_overlap"] = "%.3f" % (title_overlap([h["title"] for h in lts], [h["title"] for h in heads]) or 0.0) if lts else ""
        if cap.remaining <= 0:
            rec["status"] = "budget"
            done[key] = rec
            save(done, order)
            continue
        if client is None:
            client = CountingClient(scorer._client(), cap)
        i = index[key]
        context = signals[max(0, i - config.CONTEXT_SIGNAL_ROWS):i]
        scored, usage = scorer.score_news([as_scorer_headline(h) for h in heads], context, client=client)
        rec["cost_usd"] = "%.6f" % usage.cost_usd
        if scored is None:
            rec["status"] = "model_failed"
            print("  %s model failed: %s" % (key[:16], usage.error[:80]))
        else:
            rec["status"] = "ok"
            rec["archive_score"] = scored.score
            rec["archive_confidence"] = scored.confidence
            print("  %s live %s archive %+d  (%d archive headlines, %d live)" % (
                key[:16], row["score"], scored.score, len(heads), int(row["new_headline_count"])))
        done[key] = rec
        save(done, order)

    save(done, order)
    rows = [done[k] for k in order if k in done]
    text = build_report(rows, len(sample), cap.used)
    with open(TXT_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--limit", type=int, default=0, help="process at most N new rows this run (0 = all)")
    return run(p.parse_args(argv).limit)


if __name__ == "__main__":
    sys.exit(main())
