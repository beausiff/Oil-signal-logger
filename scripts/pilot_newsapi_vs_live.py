"""Pilot: can NewsAPI.ai (Event Registry) replace Google News RSS as the news source?

    python -m scripts.pilot_newsapi_vs_live --probe-only   # step 0 only: ONE request
    python -m scripts.pilot_newsapi_vs_live --limit 3      # smoke test
    python -m scripts.pilot_newsapi_vs_live                # the full sample

For recent rows logged LIVE, rebuild each row's news window (v2 rows: the live
rule, minutes since the previous logged row + 10 clamped to [90, 720]; other
rows: 90 minutes) from NewsAPI.ai, apply the live path's normalise / dedupe /
"40 newest" steps, score with src.scorer.score_news with the same context rows
as the earlier pilots and compare with the live score.

Fetch plan (tokens are scarce: FREE tier, 2,000 one time, 1 request = 1 token):
one UTC day at a time, newest first, paging until a page's oldest article is
older than the earliest window start needed on that day, or PAGE_LIMIT pages.
Every finished day is cached to backtest/pilot_newsapi_cache/<day>.jsonl and
never refetched. Days cut short by the page limit are flagged truncated.

HARD CAPS, both counted in files and enforced in code before the call is made:
  MAX_REQUESTS = 300 NewsAPI.ai requests  (backtest/pilot_newsapi_requests_used.txt)
  MAX_CALLS    = 60 model requests        (backtest/pilot_newsapi_calls_used.txt)

The API key (env NEWSAPI_AI_KEY) is sent in the JSON body only. It is never
printed, written to a file or put in a URL; every printed/written string passes
through redact().

VERDICT RULE (fixed before running):
  GOOD ENOUGH:      Pearson r >= 0.60 AND share of pairs within 2 points >= 70%
  NOT GOOD ENOUGH:  r < 0.40
  BORDERLINE:       anything else
  NO VERDICT:       fewer than 30 pairs
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src import config, news, stats
from scripts.pilot_archive_vs_live import (  # reused helpers
    CountingClient, SpendCap, SpendCapExceeded, evenly, title_overlap, RETEST,
)

ENDPOINT = "https://eventregistry.org/api/v1/article/getArticles"
KEY_ENV = "NEWSAPI_AI_KEY"
NO_KEY_LINE = "NO KEY: secret NEWSAPI_AI_KEY is not set"

MAX_REQUESTS = 300
MAX_CALLS = 60
MAX_ROWS = 56
SAMPLE_DAYS = 25               # rows from the last 25 days before the run date
PAGE_SIZE = 100
PAGE_LIMIT = 6                 # per UTC day
MIN_PAIRS = 30
KEEP_HEADLINES = config.MAX_HEADLINES_PER_RUN   # 40
V1_WINDOW_MINUTES = config.NEWS_LOOKBACK_MINUTES  # 90
SPACING = 1.0
MAX_RUNTIME_MINUTES = 35
KEY_SOURCES = ("Reuters", "Bloomberg", "CNBC", "OilPrice.com")

BACKTEST = "backtest"
CSV_PATH = os.path.join(BACKTEST, "pilot_newsapi_vs_live.csv")
TXT_PATH = os.path.join(BACKTEST, "pilot_newsapi_vs_live.txt")
CALLS_PATH = os.path.join(BACKTEST, "pilot_newsapi_calls_used.txt")
REQUESTS_PATH = os.path.join(BACKTEST, "pilot_newsapi_requests_used.txt")
CACHE_DIR = os.path.join(BACKTEST, "pilot_newsapi_cache")
ANCHOR_PATH = os.path.join(CACHE_DIR, "anchor.txt")
FIELDS = ["run_utc", "status", "rules_version", "window_min", "live_score", "newsapi_score",
          "live_confidence", "newsapi_confidence", "live_headlines", "newsapi_in_window",
          "newsapi_headlines", "title_overlap", "days", "truncated_day", "coverage_gap",
          "cost_usd", "top_live_titles", "top_newsapi_titles"]


# ------------------------------------------------------------ secret hygiene --

def get_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def redact(text, key: Optional[str] = None) -> str:
    """Remove the key from any string before it is printed or written."""
    s = str(text)
    k = get_key() if key is None else key
    if k:
        s = s.replace(k, "***")
    return s


def say(text="") -> None:
    print(redact(text), flush=True)


# ------------------------------------------------------------ pure helpers --

def verdict(r: Optional[float], within2: float, n: int) -> str:
    """The rule, fixed in advance."""
    if n < MIN_PAIRS or r is None:
        return "NO VERDICT (fewer than %d pairs or undefined r)" % MIN_PAIRS
    if r >= 0.60 and within2 >= 0.70:
        return "GOOD ENOUGH"
    if r < 0.40:
        return "NOT GOOD ENOUGH"
    return "BORDERLINE"


def keywords() -> List[str]:
    """The live feed's search terms (src.config.SEARCH_TERMS); multi word terms stay phrases."""
    return list(config.SEARCH_TERMS)


def build_body(key: str, day: date, page: int) -> dict:
    d = day.isoformat()
    return {
        "action": "getArticles",
        "keyword": keywords(),
        "keywordOper": "or",
        "keywordLoc": "title",
        "lang": "eng",
        "dateStart": d,
        "dateEnd": d,
        "articlesSortBy": "date",
        "articlesSortByAsc": False,
        "articlesCount": PAGE_SIZE,
        "articlesPage": page,
        "resultType": "articles",
        "isDuplicateFilter": "skipDuplicates",
        "dataType": ["news"],
        "apiKey": key,
    }


def news_window_minutes(rules_version: str, gap_minutes: Optional[float]) -> int:
    """v2 rows: the live rule (src.main.news_window_minutes). Everything else: 90 minutes."""
    if str(rules_version or "").startswith("v2"):
        from src.main import news_window_minutes as live_rule
        return live_rule(gap_minutes)
    return V1_WINDOW_MINUTES


def row_window(signals: Sequence[dict], i: int) -> Tuple[datetime, datetime, int]:
    """(start, end, minutes) of row i's news window; the window ends at run_utc."""
    run = datetime.fromisoformat(signals[i]["run_utc"])
    gap = None
    if i > 0:
        gap = (run - datetime.fromisoformat(signals[i - 1]["run_utc"])).total_seconds() / 60.0
    minutes = news_window_minutes(signals[i].get("rules_version", ""), gap)
    return run - timedelta(minutes=minutes), run, minutes


def utc_days(start: datetime, end: datetime) -> List[date]:
    d0, d1 = start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()
    out, d = [], d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def day_needs(windows: Sequence[Tuple[datetime, datetime]]) -> Dict[date, datetime]:
    """For each UTC day, the earliest instant on that day any window needs (>= midnight)."""
    need: Dict[date, datetime] = {}
    for start, end in windows:
        for d in utc_days(start, end):
            midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            s = max(start, midnight)
            if d not in need or s < need[d]:
                need[d] = s
    return need


def page_decision(page: int, pages_total: Optional[int], n_results: int,
                  page_min_pub: Optional[datetime], need_start: datetime,
                  page_limit: int = PAGE_LIMIT) -> str:
    """After fetching `page`: 'done' (day covered), 'truncated' (stopped by the limit) or 'more'."""
    if n_results == 0:
        return "done"
    if pages_total is not None and page >= pages_total:
        return "done"
    if page_min_pub is not None and page_min_pub < need_start:
        return "done"
    if page >= page_limit:
        return "truncated"
    return "more"


def eligible(row: dict, lo_day: str, hi_day: str) -> bool:
    """Live scored rows, as the earlier pilots treat them (score present, not backfilled/carried,
    at least 5 live headlines), run_utc day in [lo_day, hi_day]."""
    day = str(row.get("run_utc", ""))[:10]
    src = str(row.get("score_source", ""))
    nh = str(row.get("new_headline_count", "")).strip()
    return (lo_day <= day <= hi_day and str(row.get("score", "")).strip() != ""
            and not src.startswith("backfilled") and src != "carried"
            and nh.isdigit() and int(nh) >= 5)


def sample_rows(signals: Sequence[dict], anchor: date) -> List[dict]:
    lo = (anchor - timedelta(days=SAMPLE_DAYS)).isoformat()
    return evenly([r for r in signals if eligible(r, lo, anchor.isoformat())], MAX_ROWS)


def art_time(a: dict) -> Tuple[Optional[datetime], str]:
    """(publish time, field used): dateTimePub preferred, dateTime as fallback."""
    for field in ("dateTimePub", "dateTime"):
        t = news._parse_iso(str(a.get(field) or ""))
        if t is not None:
            return t, field
    return None, ""


def to_headline(a: dict) -> Optional[news.Headline]:
    """Same normalise step as the live path (_clean_title, headline_hash)."""
    t, _ = art_time(a)
    title = news._clean_title(a.get("title") or "")
    if t is None or not title:
        return None
    source = str(a.get("source") or "unknown").strip() or "unknown"
    return news.Headline(hash=news.headline_hash(title, source), published_utc=t.isoformat(),
                         source=source, title=title, url=a.get("url") or "")


def window_articles(articles: Sequence[dict], start: datetime, end: datetime) -> List[news.Headline]:
    """Articles whose publish time lies in [start, end], as Headlines."""
    out = []
    for a in articles:
        t, _ = art_time(a)
        if t is not None and start <= t <= end:
            h = to_headline(a)
            if h is not None:
                out.append(h)
    return out


def final_headlines(inside: Sequence[news.Headline], run_dt: datetime, minutes: int) -> List[news.Headline]:
    """Live dedupe (by hash) and "40 newest" step."""
    return news.filter_and_dedupe(inside, [], run_dt, lookback_minutes=minutes, cap=KEEP_HEADLINES)


# ------------------------------------------------------------ request cap ----

class RequestCapExceeded(RuntimeError):
    pass


class RequestCap(SpendCap):
    """Counts NewsAPI.ai requests in a file; the 301st is refused in code."""

    def consume(self) -> None:
        if self.used >= self.max_calls:
            raise RequestCapExceeded("request cap of %d NewsAPI.ai requests reached" % self.max_calls)
        self.used += 1
        with open(self.path, "w") as fh:
            fh.write("%d\n" % self.used)  # written BEFORE the request is made


# ------------------------------------------------------------------- API -----

_last_request = [0.0]


def api_post(body: dict, cap: RequestCap, session=None, sleep=time.sleep, clock=time.time) -> Optional[dict]:
    """One counted request. Returns the decoded JSON, or None after printing status + 300 chars of body.

    One retry only for a network error or a 5xx (each attempt spends a counted request)."""
    import requests
    session = session or requests.Session()
    key = body.get("apiKey", "")
    for attempt in (1, 2):
        cap.consume()
        wait = SPACING - (clock() - _last_request[0])
        if wait > 0:
            sleep(wait)
        _last_request[0] = clock()
        status, text, data = None, "", None
        try:
            resp = session.post(ENDPOINT, json=body, timeout=60)
            status, text = resp.status_code, resp.text or ""
            if status == 200:
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            status, text = type(exc).__name__, redact(str(exc), key)
        if isinstance(data, dict) and not data.get("error"):
            return data
        if isinstance(data, dict):
            text = json.dumps(data)
        say("  newsapi request failed: status %s body %s" % (status, redact(text, key)[:300]))
        retryable = isinstance(status, str) or (isinstance(status, int) and status >= 500)
        if not retryable or attempt == 2:
            return None
        sleep(5)
    return None


def parse_page(data: dict) -> dict:
    """Pull the article list and paging info from a getArticles response."""
    block = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return {"results": [], "total": None, "pages": None}
    results = block.get("results")
    return {"results": results if isinstance(results, list) else [],
            "total": block.get("totalResults"), "pages": block.get("pages")}


def slim(a: dict) -> dict:
    src = a.get("source")
    name = (src.get("title") or src.get("uri") or "") if isinstance(src, dict) else (src or "")
    return {"title": a.get("title") or "", "source": name, "url": a.get("url") or "",
            "dateTime": a.get("dateTime") or "", "dateTimePub": a.get("dateTimePub") or ""}


def probe(cap: RequestCap, day: date, key: str, session=None) -> Optional[dict]:
    """Step 0: ONE request. Print the true response shape (never the key)."""
    say("== STEP 0: probe (1 request, day %s) ==" % day)
    data = api_post(build_body(key, day, 1), cap, session=session)
    if data is None:
        return None
    page = parse_page(data)
    arts = page["results"]
    say("top level keys: %s" % sorted(data.keys()))
    block = data.get("articles")
    say("articles block keys: %s" % (sorted(block.keys()) if isinstance(block, dict) else type(block).__name__))
    say("article field names: %s" % (sorted(arts[0].keys()) if arts else "(no articles)"))
    if arts and isinstance(arts[0].get("source"), dict):
        say("source field names: %s" % sorted(arts[0]["source"].keys()))
    say("totalResults: %s   pages: %s   articles on page 1: %d" % (page["total"], page["pages"], len(arts)))
    if arts:
        for label, a in (("first", arts[0]), ("last", arts[-1])):
            say("%s article: dateTime=%s dateTimePub=%s" % (label, a.get("dateTime"), a.get("dateTimePub")))
    return data


def fetch_day(day: date, need_start: datetime, key: str, cap: RequestCap, session=None) -> Optional[dict]:
    """Page one UTC day newest first. Returns {"meta":..., "articles":[slim...]} or None on failure.

    A RequestCapExceeded propagates (the caller stops cleanly)."""
    articles: List[dict] = []
    total, pages_total, truncated, fetched = None, None, False, 0
    for page in range(1, PAGE_LIMIT + 1):
        data = api_post(build_body(key, day, page), cap, session=session)
        if data is None:
            return None
        p = parse_page(data)
        fetched = page
        if page == 1:
            total, pages_total = p["total"], p["pages"]
        articles.extend(slim(a) for a in p["results"])
        times = [t for t in (art_time(a)[0] for a in p["results"]) if t is not None]
        decision = page_decision(page, pages_total, len(p["results"]), min(times) if times else None, need_start)
        if decision == "truncated":
            truncated = True
        if decision != "more":
            break
    pubs = [t for t in (art_time(a)[0] for a in articles) if t is not None]
    meta = {"day": day.isoformat(), "total_results": total, "pages_total": pages_total,
            "pages_fetched": fetched, "truncated": truncated, "articles": len(articles),
            "floor_pub": min(pubs).isoformat() if pubs else "",
            "need_start": need_start.isoformat()}
    return {"meta": meta, "articles": articles}


# ------------------------------------------------------------------ cache ----

def cache_path(day: date, cache_dir: str = CACHE_DIR) -> str:
    return os.path.join(cache_dir, "%s.jsonl" % day.isoformat())


def save_day(day: date, result: dict, cache_dir: str = CACHE_DIR) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_path(day, cache_dir), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": result["meta"]}) + "\n")
        for a in result["articles"]:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")


def load_day(day: date, cache_dir: str = CACHE_DIR) -> Optional[dict]:
    path = cache_path(day, cache_dir)
    if not os.path.exists(path):
        return None
    meta, arts = None, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            obj = json.loads(line)
            if "_meta" in obj:
                meta = obj["_meta"]
            else:
                arts.append(obj)
    return {"meta": meta or {}, "articles": arts} if meta else None


class DayStore:
    """Each day is fetched at most once, ever: memory, then the committed cache, then the API."""

    def __init__(self, needs: Dict[date, datetime], key: str, cap: RequestCap, session=None,
                 cache_dir: str = CACHE_DIR, fetch=fetch_day):
        self.needs, self.key, self.cap, self.session = needs, key, cap, session
        self.cache_dir, self._fetch = cache_dir, fetch
        self._mem: Dict[date, Optional[dict]] = {}

    def get(self, day: date) -> Optional[dict]:
        if day in self._mem:
            return self._mem[day]
        res = load_day(day, self.cache_dir)
        if res is None:
            res = self._fetch(day, self.needs[day], self.key, self.cap, self.session)
            if res is not None:
                save_day(day, res, self.cache_dir)
        self._mem[day] = res
        return res


def coverage(days: Sequence[Optional[dict]], start: datetime) -> Tuple[bool, bool]:
    """(touches a truncated day, window start is before a truncated day's coverage floor)."""
    trunc = gap = False
    for d in days:
        if not d or not d["meta"].get("truncated"):
            continue
        trunc = True
        floor = news._parse_iso(d["meta"].get("floor_pub", ""))
        day = datetime.fromisoformat(d["meta"]["day"]).replace(tzinfo=timezone.utc)
        if floor is None or max(start, day) < floor:
            gap = True
    return trunc, gap


# ----------------------------------------------------------------- report ----

def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pair_stats(rows: Sequence[dict]) -> Optional[dict]:
    ok = [r for r in rows if r.get("status") == "ok"]
    n = len(ok)
    if not n:
        return None
    live = [float(r["live_score"]) for r in ok]
    new = [float(r["newsapi_score"]) for r in ok]
    diffs = [abs(a - b) for a, b in zip(live, new)]
    r = stats.pearson(live, new)
    ci = stats.fisher_ci(r, n) if r is not None and n >= 4 else None
    return {"n": n, "r": r, "ci": ci, "mad": sum(diffs) / n,
            "same": sum(d == 0 for d in diffs) / n, "w1": sum(d <= 1 for d in diffs) / n,
            "w2": sum(d <= 2 for d in diffs) / n, "mean_live": sum(live) / n, "mean_new": sum(new) / n}


def article_stats(days: Sequence[dict]) -> dict:
    arts = [a for d in days for a in d["articles"]]
    times = [art_time(a) for a in arts]
    pub_missing = sum(1 for a in arts if not news._parse_iso(str(a.get("dateTimePub") or "")))
    used_pub = sum(1 for _, f in times if f == "dateTimePub")
    minute_level = sum(1 for t, _ in times if t is not None and (t.hour or t.minute or t.second))
    timed = sum(1 for t, _ in times if t is not None)
    lags = []
    for a in arts:
        p, c = news._parse_iso(str(a.get("dateTimePub") or "")), news._parse_iso(str(a.get("dateTime") or ""))
        if p is not None and c is not None:
            lags.append((c - p).total_seconds() / 60.0)
    srcs = Counter(str(a.get("source") or "unknown") for a in arts)
    return {"total": len(arts), "pub_missing": pub_missing, "used_pub": used_pub, "timed": timed,
            "minute_level": minute_level, "lag_median": statistics.median(lags) if lags else None,
            "lag_n": len(lags), "sources": srcs}


def build_report(rows: List[dict], sampled: int, requests_used: int, calls_used: int,
                 day_metas: Sequence[dict], art: Optional[dict], window_counts: Dict[str, float]) -> str:
    out: List[str] = []
    w = out.append
    ps = pair_stats(rows)
    n = ps["n"] if ps else 0
    w("PILOT: NewsAPI.ai (Event Registry) versus the live Google News RSS feed")
    w("Rule (fixed before running): GOOD ENOUGH = Pearson r >= 0.60 AND >= 70%% of pairs within 2 points; "
      "NOT GOOD ENOUGH = r < 0.40; BORDERLINE = anything else; NO VERDICT = fewer than %d pairs." % MIN_PAIRS)
    w("Context: " + RETEST + ".")
    w("Query: live search terms as keywords (OR, title only, English, skipDuplicates), one UTC day per fetch, "
      "newest first, up to %d pages of %d per day. Window: v2 rows = live rule (gap + 10 min, clamped 90..720), "
      "other rows 90 min, ending at run_utc." % (PAGE_LIMIT, PAGE_SIZE))
    w("")
    w("rows sampled: %d" % sampled)
    w("pairs scored: %d of %d rows sampled" % (n, sampled))
    w("rows with zero headlines in window: %d" % sum(1 for r in rows if r.get("status") == "no_headlines"))
    w("rows affected by truncated days (window touches a day cut short by the page limit): %d"
      % sum(1 for r in rows if str(r.get("truncated_day")) == "1"))
    w("  of which the window starts before that day's oldest fetched article (data really missing): %d"
      % sum(1 for r in rows if str(r.get("coverage_gap")) == "1"))
    w("rows skipped for NewsAPI.ai fetch failure or request cap: %d"
      % sum(1 for r in rows if r.get("status") in ("fetch_failed", "request_cap")))
    w("rows not scored for model failure or spend cap: %d"
      % sum(1 for r in rows if r.get("status") in ("model_failed", "budget")))
    w("rows not yet processed: %d" % (sampled - len(rows)))
    if ps:
        w("Pearson r (newsapi vs live): %s" % ("n/a" if ps["r"] is None else "%.3f" % ps["r"]))
        w("Fisher 95%% interval for r: %s" % ("n/a" if ps["ci"] is None else "[%.3f, %.3f]" % ps["ci"]))
        w("mean absolute difference: %.2f points" % ps["mad"])
        w("identical: %.0f%%   within 1: %.0f%%   within 2: %.0f%%" % (100 * ps["same"], 100 * ps["w1"], 100 * ps["w2"]))
        w("mean score: live %.2f, NewsAPI %.2f" % (ps["mean_live"], ps["mean_new"]))
        clean = [r for r in rows if r.get("status") == "ok" and str(r.get("coverage_gap")) != "1"]
        pc = pair_stats(clean)
        if pc and len(clean) != n:
            w("excluding rows with a real coverage gap: n=%d r=%s within 2: %.0f%%"
              % (pc["n"], "n/a" if pc["r"] is None else "%.3f" % pc["r"], 100 * pc["w2"]))
        ok = [r for r in rows if r.get("status") == "ok"]
        lh = [float(x["live_headlines"]) for x in ok]
        nh = [float(x["newsapi_headlines"]) for x in ok]
        w("average headline count: live %.1f; NewsAPI.ai %.1f after dedupe and the 40 cap, %.1f in window before them"
          % (sum(lh) / n, sum(nh) / n, window_counts.get("before_cap", float("nan"))))
        ov = [float(x["title_overlap"]) for x in ok if x.get("title_overlap") not in ("", None)]
        w("average title overlap with logged live headlines (share with a >=60%% Jaccard match): %s"
          % ("%.0f%%" % (100 * sum(ov) / len(ov)) if ov else "n/a"))
    w("")
    w("VERDICT: %s" % verdict(ps["r"] if ps else None, ps["w2"] if ps else 0.0, n))
    w("")
    if art:
        w("== article data (all cached articles of the sampled days: %d) ==" % art["total"])
        w("publish time used: dateTimePub for %d articles, dateTime fallback for %d; dateTimePub missing on %d (%.1f%%)"
          % (art["used_pub"], art["timed"] - art["used_pub"], art["pub_missing"],
             100.0 * art["pub_missing"] / art["total"] if art["total"] else 0.0))
        w("share with minute level publish times (not exactly midnight): %.1f%%"
          % (100.0 * art["minute_level"] / art["timed"] if art["timed"] else 0.0))
        w("median lag dateTime minus dateTimePub: %s (n=%d)"
          % ("n/a" if art["lag_median"] is None else "%.1f min" % art["lag_median"], art["lag_n"]))
        w("top 15 sources by article count:")
        for name, c in art["sources"].most_common(15):
            w("  %5d  %s" % (c, name))
        low = {k: [s for s in art["sources"] if k.lower() in s.lower()] for k in KEY_SOURCES}
        for k in KEY_SOURCES:
            w("  %s: %s" % (k, ("present (%s)" % ", ".join("%s %d" % (s, art["sources"][s]) for s in low[k][:4]))
                             if low[k] else "absent"))
        w("")
    w("== articles per sampled day (totalResults from the API; fetched; pages; truncated) ==")
    for m in sorted(day_metas, key=lambda x: x.get("day", "")):
        w("%s  totalResults %s  fetched %s  pages %s/%s  truncated %s  oldest fetched %s" % (
            m.get("day"), m.get("total_results"), m.get("articles"), m.get("pages_fetched"),
            m.get("pages_total"), "YES" if m.get("truncated") else "no", (m.get("floor_pub") or "-")[:16]))
    w("")
    spend = sum(_f(x.get("cost_usd")) or 0.0 for x in rows)
    w("NewsAPI.ai requests used: %d of %d" % (requests_used, MAX_REQUESTS))
    w("model calls used: %d of %d   model spend (scorer usage cost): $%.4f" % (calls_used, MAX_CALLS, spend))
    w("")
    w("== 5 example rows (evenly spaced through the pairs) ==")
    for x in evenly([r for r in rows if r.get("status") == "ok"], 5):
        w("%s  live %+d (%s)  newsapi %+d (%s)  overlap %s  window %s min" % (
            x["run_utc"][:16], int(float(x["live_score"])), x["live_confidence"],
            int(float(x["newsapi_score"])), x["newsapi_confidence"], x.get("title_overlap", ""),
            x.get("window_min", "")))
        w("  live:    " + (x.get("top_live_titles") or "(none)"))
        w("  newsapi: " + (x.get("top_newsapi_titles") or "(none)"))
    return redact("\n".join(out)) + "\n"


# ------------------------------------------------------------------ main ----

def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(redact(text))


def load_csv(path: str = CSV_PATH) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["run_utc"]: r for r in csv.DictReader(fh)}


def save_csv(done: Dict[str, dict], order: List[str], path: str = CSV_PATH) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for k in order:
            if k in done:
                w.writerow({f: redact(done[k].get(f, "")) for f in FIELDS})


def get_anchor(today: date) -> date:
    """The sample's anchor date, fixed on the first run so reruns use the same rows."""
    try:
        return date.fromisoformat(open(ANCHOR_PATH).read().strip())
    except (OSError, ValueError):
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(ANCHOR_PATH, "w") as fh:
            fh.write(today.isoformat() + "\n")
        return today


def run(limit: int, probe_only: bool) -> int:
    os.makedirs(BACKTEST, exist_ok=True)
    key = get_key()
    if not key:
        write_text(TXT_PATH, NO_KEY_LINE + "\n")
        say(NO_KEY_LINE)
        return 0

    with open("backup/signals.csv", newline="", encoding="utf-8") as fh:
        signals = sorted(csv.DictReader(fh), key=lambda r: r["run_utc"])
    index = {r["run_utc"]: i for i, r in enumerate(signals)}
    anchor = get_anchor(datetime.now(timezone.utc).date())
    sample = sample_rows(signals, anchor)
    order = [r["run_utc"] for r in sample]
    wins = {r["run_utc"]: row_window(signals, index[r["run_utc"]]) for r in sample}
    needs = day_needs([(s, e) for s, e, _ in wins.values()])
    live_titles: Dict[str, List[dict]] = {}
    with open("backup/headlines.csv", newline="", encoding="utf-8") as fh:
        for h in csv.DictReader(fh):
            live_titles.setdefault(h["run_utc"], []).append(h)

    req_cap = RequestCap(REQUESTS_PATH, MAX_REQUESTS)
    cap = SpendCap(CALLS_PATH, MAX_CALLS)
    say("sample %d rows (last %d days before %s), %d days needed, requests used %d of %d, model calls used %d of %d" % (
        len(sample), SAMPLE_DAYS, anchor, len(needs), req_cap.used, req_cap.max_calls, cap.used, cap.max_calls))
    if not sample:
        write_text(TXT_PATH, "NO ROWS: no eligible live rows in the last %d days before %s\n" % (SAMPLE_DAYS, anchor))
        return 0

    try:
        if probe_only:
            last = max(needs)
            data = probe(req_cap, last, key)
            text = "PROBE ONLY (1 request, day %s): %s\n" % (last, "ok, see the job log for the response shape"
                                                              if data is not None else "FAILED, see the job log")
            if data is not None:
                page = parse_page(data)
                text += "totalResults %s, pages %s, articles on page 1 %d\nNewsAPI.ai requests used: %d of %d\n" % (
                    page["total"], page["pages"], len(page["results"]), req_cap.used, MAX_REQUESTS)
            write_text(TXT_PATH, text)
            return 0
    except RequestCapExceeded as exc:
        say(str(exc))
        return 0

    store = DayStore(needs, key, req_cap)
    done = load_csv()
    client = None
    processed = 0
    t0 = time.time()
    for row in sample:
        runk = row["run_utc"]
        if done.get(runk, {}).get("status") in ("ok", "no_headlines"):
            continue
        if limit and processed >= limit:
            break
        if time.time() - t0 > MAX_RUNTIME_MINUTES * 60:
            say("runtime budget reached, stopping; re-dispatch to resume")
            break
        processed += 1
        start, run_dt, minutes = wins[runk]
        lts = sorted(live_titles.get(runk, []), key=lambda h: h.get("published_utc", ""), reverse=True)
        rec = {"run_utc": runk, "status": "", "rules_version": row.get("rules_version", ""),
               "window_min": minutes, "live_score": row["score"], "live_confidence": row["confidence"],
               "live_headlines": row["new_headline_count"],
               "top_live_titles": " | ".join(h["title"] for h in lts[:3])}
        days = utc_days(start, run_dt)
        rec["days"] = " ".join(d.isoformat() for d in days)
        loaded, failed = [], False
        for d in days:
            try:
                res = store.get(d)
            except RequestCapExceeded as exc:
                say(str(exc))
                rec["status"] = "request_cap"
                failed = True
                break
            if res is None:
                rec["status"] = "fetch_failed"
                failed = True
                break
            loaded.append(res)
        if failed:
            done[runk] = rec
            save_csv(done, order)
            say("  %s %s, row skipped" % (runk[:16], rec["status"]))
            continue
        trunc, gap = coverage(loaded, start)
        rec["truncated_day"], rec["coverage_gap"] = int(trunc), int(gap)
        arts = [a for d in loaded for a in d["articles"]]
        inside = window_articles(arts, start, run_dt)
        heads = final_headlines(inside, run_dt, minutes)
        rec["newsapi_in_window"] = len(inside)
        rec["newsapi_headlines"] = len(heads)
        rec["top_newsapi_titles"] = " | ".join(h.title for h in heads[:3])
        if not heads:
            rec["status"] = "no_headlines"
            done[runk] = rec
            save_csv(done, order)
            say("  %s zero headlines in window" % runk[:16])
            continue
        if lts:
            rec["title_overlap"] = "%.3f" % (title_overlap([h["title"] for h in lts], [h.title for h in heads]) or 0.0)
        if cap.remaining <= 0:
            rec["status"] = "budget"
            done[runk] = rec
            save_csv(done, order)
            continue
        from src import scorer
        if client is None:
            client = CountingClient(scorer._client(), cap)
        i = index[runk]
        context = signals[max(0, i - config.CONTEXT_SIGNAL_ROWS):i]
        kw = {"window_minutes": minutes} if str(row.get("rules_version", "")).startswith("v2") else {}
        scored, usage = scorer.score_news([h.as_dict() for h in heads], context, client=client, **kw)
        rec["cost_usd"] = "%.6f" % usage.cost_usd
        if scored is None:
            rec["status"] = "model_failed"
            say("  %s model failed: %s" % (runk[:16], usage.error[:80]))
        else:
            rec["status"] = "ok"
            rec["newsapi_score"] = scored.score
            rec["newsapi_confidence"] = scored.confidence
            say("  %s live %s newsapi %+d  (%d newsapi headlines, %d live, window %d min%s)" % (
                runk[:16], row["score"], scored.score, len(heads), int(row["new_headline_count"]), minutes,
                ", TRUNCATED day" if trunc else ""))
        done[runk] = rec
        save_csv(done, order)

    save_csv(done, order)
    rows = [done[k] for k in order if k in done]
    # Report inputs come from the cache, so a resumed run reports the whole sample.
    all_days = [d for d in (load_day(x) for x in sorted(needs)) if d]
    metas = [d["meta"] for d in all_days]
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    wc = {}
    vals = [float(r["newsapi_in_window"]) for r in ok_rows if r.get("newsapi_in_window") not in ("", None)]
    if vals:
        wc["before_cap"] = sum(vals) / len(vals)
    text = build_report(rows, len(sample), req_cap.used, cap.used, metas,
                        article_stats(all_days) if all_days else None, wc)
    write_text(TXT_PATH, text)
    say(text)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--limit", type=int, default=0, help="process at most N new rows this run (0 = all)")
    p.add_argument("--probe-only", action="store_true", help="step 0 only: one request, print the response shape")
    a = p.parse_args(argv)
    return run(a.limit, a.probe_only)


if __name__ == "__main__":
    sys.exit(main())
