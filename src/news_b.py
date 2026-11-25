"""Feed B news: NewsAPI.ai (Event Registry) instead of Google News RSS.

Isolated from src/news.py on purpose. Only the live path's pure helpers
(title normalising, hashing, dedupe) are imported from there, so both feeds
clean headlines identically.

The API key (env NEWSAPI_AI_KEY) goes in the JSON body only. It is never put in
a URL, printed, logged or written to the sheet or a file: every string that
leaves this module passes through redact().

Budget: the account is a FREE tier with a one time allowance of search tokens
(one request = one token = up to 100 articles). Two limits, both enforced here
in code, before the request is made:
  * MAX_REQUESTS_PER_RUN requests in one feed B run,
  * LIFETIME_CAP requests over the whole life of feed B (persisted by the caller
    in the sheet tab feed_b_state).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import config, news

ENDPOINT = "https://eventregistry.org/api/v1/article/getArticles"
KEY_ENV = "NEWSAPI_AI_KEY"
PAGE_SIZE = 100
MAX_REQUESTS_PER_RUN = 2
LIFETIME_CAP = 1500
HTTP_TIMEOUT_SECONDS = 60
KEEP_HEADLINES = config.MAX_HEADLINES_PER_RUN   # 40, as the live path

# ----------------------------------------------------------- source filter ----
# Decided ONCE, before the twin bots start, by the probe in scripts/feed_b_probe.py
# (workflow feed_b, mode probe_filter) under a rule fixed in advance: use the
# tightest Event Registry source rank percentile filter (end percentile 30, else
# 50) for which the API accepts the fields AND the last 90 minutes hold at least
# 15 articles; otherwise no filter. The decision is locked below with the date
# and the evidence. Until it is locked feed B refuses to run, so a run can never
# start on an undecided filter.
SOURCE_FILTER_LOCKED = False
SOURCE_RANK_END_PERCENTILE: Optional[int] = None   # None = no filter; 30 or 50 = endSourceRankPercentile
SOURCE_RANK_START_PERCENTILE = 0
FILTER_DECISION = "UNDECIDED"

SECRET_ENVS = ("NEWSAPI_AI_KEY", "ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON")


# ------------------------------------------------------------ secret hygiene --

def _secret_values() -> List[str]:
    values = []
    for name in SECRET_ENVS:
        raw = os.environ.get(name, "").strip()
        if len(raw) >= 6:
            values.append(raw)
        if name == "GOOGLE_SERVICE_ACCOUNT_JSON" and raw:
            try:
                info = json.loads(raw)
                for field_name in ("private_key", "private_key_id"):
                    v = str(info.get(field_name, "") or "")
                    if len(v) >= 6:
                        values.append(v)
            except (ValueError, AttributeError):
                pass
    return values


def redact(text) -> str:
    """Remove every known secret from a string before it is printed or written."""
    s = str(text)
    for value in _secret_values():
        s = s.replace(value, "***")
    return s


def say(text="") -> None:
    print(redact(text), flush=True)


def get_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


# ----------------------------------------------------------------- budget ----

class CapReached(RuntimeError):
    """The lifetime cap of NewsAPI.ai requests is used up."""


class RunLimit(RuntimeError):
    """This run has used its request allowance."""


class Budget:
    """Counts requests. consume() persists the new count BEFORE the request is made.

    `used` is the lifetime count; `persist(n)` writes it somewhere durable (the
    sheet). If persisting fails the exception propagates and no request is made,
    so a request can never go uncounted.
    """

    def __init__(self, used: int, persist: Callable[[int], None],
                 cap: int = LIFETIME_CAP, per_run: int = MAX_REQUESTS_PER_RUN):
        self.used = int(used)
        self.cap = cap
        self.per_run = per_run
        self.run_used = 0
        self._persist = persist

    def consume(self) -> None:
        if self.used >= self.cap:
            raise CapReached("lifetime cap of %d NewsAPI.ai requests reached" % self.cap)
        if self.run_used >= self.per_run:
            raise RunLimit("run limit of %d requests reached" % self.per_run)
        self._persist(self.used + 1)
        self.used += 1
        self.run_used += 1


# ------------------------------------------------------------------- request --

def utc_days_between(start: datetime, end: datetime) -> Tuple[date, date]:
    return start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()


def build_body(key: str, day_start: date, day_end: date, page: int,
               end_percentile: Optional[int] = None,
               start_percentile: int = SOURCE_RANK_START_PERCENTILE) -> dict:
    """Newest first, the live feed's search terms in the title, English, no duplicates."""
    body = {
        "action": "getArticles",
        "keyword": list(config.SEARCH_TERMS),
        "keywordOper": "or",
        "keywordLoc": "title",
        "lang": "eng",
        "dateStart": day_start.isoformat(),
        "dateEnd": day_end.isoformat(),
        "articlesSortBy": "date",
        "articlesSortByAsc": False,
        "articlesCount": PAGE_SIZE,
        "articlesPage": page,
        "resultType": "articles",
        "isDuplicateFilter": "skipDuplicates",
        "dataType": ["news"],
        "apiKey": key,
    }
    if end_percentile is not None:
        body["startSourceRankPercentile"] = start_percentile
        body["endSourceRankPercentile"] = end_percentile
    return body


def post(body: dict, session=None) -> Tuple[Optional[dict], str]:
    """One HTTP request. Returns (decoded JSON, "") or (None, redacted reason). Never raises, never retries."""
    import requests
    session = session or requests.Session()
    try:
        resp = session.post(ENDPOINT, json=body, timeout=HTTP_TIMEOUT_SECONDS)
        status, text = resp.status_code, resp.text or ""
        data = resp.json() if status == 200 else None
    except Exception as exc:  # noqa: BLE001 - network, TLS, bad JSON
        return None, redact("%s: %s" % (type(exc).__name__, exc))[:200]
    if isinstance(data, dict) and not data.get("error"):
        return data, ""
    if isinstance(data, dict):
        text = json.dumps(data)
    return None, redact("status %s body %s" % (status, text))[:300]


def parse_page(data: dict) -> dict:
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


def index_time(a: dict) -> Optional[datetime]:
    return news._parse_iso(str(a.get("dateTime") or ""))


def pub_time(a: dict) -> Optional[datetime]:
    """Publish time, falling back to the index time when it is missing."""
    for name in ("dateTimePub", "dateTime"):
        t = news._parse_iso(str(a.get(name) or ""))
        if t is not None:
            return t
    return None


# --------------------------------------------------------------- the window --

@dataclass
class FetchResult:
    articles: List[dict] = field(default_factory=list)   # slim dicts, newest first
    requests: int = 0
    cut_short: bool = False     # the request limit stopped paging before the window start was reached
    error: str = ""             # non empty: no usable result, the caller logs a blank score row
    cap_reached: bool = False
    total: Optional[int] = None


def fetch_window(start: datetime, end: datetime, key: str, budget: Budget, session=None,
                 end_percentile: Optional[int] = None,
                 poster: Callable = post) -> FetchResult:
    """Page newest first over the UTC day(s) the window touches.

    Stops when a page's oldest INDEX time (dateTime, the field the API sorts by)
    is older than the window start, when the API has no more pages, or when the
    budget refuses another request (cut_short is then set).
    """
    result = FetchResult()
    day_start, day_end = utc_days_between(start, end)
    page = 0
    while True:
        page += 1
        try:
            budget.consume()
        except CapReached as exc:
            if page == 1:
                result.error = "cap_reached: %s" % exc
                result.cap_reached = True
            else:
                result.cut_short = True
                result.cap_reached = True
            return result
        except RunLimit:
            result.cut_short = True
            return result
        except Exception as exc:  # noqa: BLE001 - the counter could not be persisted: do not spend
            result.error = redact("counter_not_saved: %s: %s" % (type(exc).__name__, exc))[:200]
            return result
        result.requests += 1
        data, err = poster(build_body(key, day_start, day_end, page, end_percentile), session)
        if data is None:
            result.error = "newsapi_error: %s" % err
            return result
        p = parse_page(data)
        if page == 1:
            result.total = p["total"]
        result.articles.extend(slim(a) for a in p["results"])
        times = [t for t in (index_time(a) for a in p["results"]) if t is not None]
        if not p["results"]:
            return result
        if p["pages"] is not None and page >= p["pages"]:
            return result
        if times and min(times) < start:
            return result
        # More to fetch: the loop asks the budget, which says no after the run limit.


def window_headlines(articles: Sequence[dict], start: datetime, end: datetime,
                     known_hashes: Sequence[str], minutes: int) -> List[news.Headline]:
    """Articles published inside [start, end], then the live path's normalise, dedupe and newest 40."""
    inside = []
    for a in articles:
        t = pub_time(a)
        title = news._clean_title(a.get("title") or "")
        if t is None or not title or not (start <= t <= end):
            continue
        source = str(a.get("source") or "unknown").strip() or "unknown"
        inside.append(news.Headline(hash=news.headline_hash(title, source),
                                    published_utc=t.isoformat(), source=source,
                                    title=title, url=a.get("url") or ""))
    return news.filter_and_dedupe(inside, known_hashes, end, lookback_minutes=minutes,
                                  cap=KEEP_HEADLINES)
