"""Feed B source filter probe: decide ONCE, under a rule fixed in advance.

    python -m scripts.feed_b_probe        # workflow feed_b, mode probe_filter

Candidate filter: Event Registry source rank percentile, request fields
startSourceRankPercentile 0 and endSourceRankPercentile 30 (then 50).

RULE, fixed before running: use the TIGHTEST filter (end percentile 30, else 50)
for which the API accepts the fields AND the last 90 minutes contain at least
MIN_ARTICLES_90 (15) articles; otherwise use no filter. "Accepted" means the
request succeeded without an error AND the filtered totalResults is smaller than
the unfiltered one taken in the same run (a silently ignored field would leave
it unchanged). The 50 variant is only tried when the 30 variant was accepted.

Spends at most MAX_PROBE_REQUESTS (12) requests, counted against the lifetime
cap in the sheet tab feed_b_state. Prints and writes only numbers and source
names, never the key. Output: backtest/feed_b_filter_probe.txt
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from src import news, news_b

MAX_PROBE_REQUESTS = 12
PAGES_PER_VARIANT = 3
MIN_ARTICLES_90 = 15
OUT_PATH = os.path.join("backtest", "feed_b_filter_probe.txt")
KEY_SOURCES = ("Reuters", "Bloomberg", "CNBC", "OilPrice.com")


def probe_variant(name: str, percentile: Optional[int], key: str, budget, now: datetime,
                  session=None, poster=news_b.post) -> dict:
    """Fetch newest first until a page's oldest index time is older than 90 minutes ago (max 3 pages)."""
    cutoff = now - timedelta(minutes=90)
    day_start, day_end = news_b.utc_days_between(cutoff - timedelta(hours=1), now)
    out = {"name": name, "percentile": percentile, "ok": False, "error": "", "total": None,
           "requests": 0, "articles": [], "last90": 0, "last90_is_lower_bound": False}
    for page in range(1, PAGES_PER_VARIANT + 1):
        try:
            budget.consume()
        except (news_b.CapReached, news_b.RunLimit) as exc:
            out["error"] = "budget: %s" % exc
            return out
        out["requests"] += 1
        data, err = poster(news_b.build_body(key, day_start, day_end, page, percentile), session)
        if data is None:
            out["error"] = err
            return out
        p = news_b.parse_page(data)
        if page == 1:
            out["total"] = p["total"]
        out["articles"].extend(news_b.slim(a) for a in p["results"])
        times = [t for t in (news_b.index_time(a) for a in p["results"]) if t is not None]
        if not p["results"] or (p["pages"] is not None and page >= p["pages"]) or (times and min(times) < cutoff):
            break
        if page == PAGES_PER_VARIANT:
            out["last90_is_lower_bound"] = True
    out["ok"] = True
    recent = [a for a in out["articles"] if (news_b.pub_time(a) or cutoff) >= cutoff]
    out["last90"] = len(recent)
    out["recent"] = recent
    return out


def accepted(filtered: dict, unfiltered: dict) -> bool:
    """The API took the fields: no error, and the filter actually narrowed the result."""
    return bool(filtered["ok"] and unfiltered["ok"] and filtered["total"] is not None
                and unfiltered["total"] is not None and filtered["total"] < unfiltered["total"])


def choose(results: Dict[str, dict]) -> Optional[int]:
    """The rule. Returns 30, 50 or None (no filter)."""
    base = results.get("none")
    for name, pct in (("p30", 30), ("p50", 50)):
        res = results.get(name)
        if res and base and accepted(res, base) and res["last90"] >= MIN_ARTICLES_90:
            return pct
    return None


def top_sources(articles: List[dict], n: int = 15) -> List[tuple]:
    return Counter((a.get("source") or "unknown") for a in articles).most_common(n)


def report(results: Dict[str, dict], decision: Optional[int], now: datetime, used_by_probe: int, lifetime: int) -> str:
    lines = ["FEED B SOURCE FILTER PROBE, %s UTC" % now.strftime("%Y-%m-%d %H:%M"),
             "Rule (fixed before running): tightest of end percentile 30, else 50, for which the API accepts the fields "
             "(no error and a smaller totalResults than unfiltered) AND the last 90 minutes hold at least %d articles; "
             "otherwise no filter." % MIN_ARTICLES_90, ""]
    for name in ("none", "p30", "p50"):
        r = results.get(name)
        if r is None:
            lines.append("== %s: not run ==" % name)
            continue
        label = "no filter" if r["percentile"] is None else "startSourceRankPercentile 0, endSourceRankPercentile %d" % r["percentile"]
        lines.append("== %s (%s) ==" % (name, label))
        lines.append("fields accepted by the API: %s" % ("yes" if r["ok"] else "NO, %s" % r["error"]))
        lines.append("totalResults for the days queried: %s" % r["total"])
        lines.append("requests used: %d" % r["requests"])
        if r["ok"]:
            lines.append("articles published in the last 90 minutes: %s%d"
                         % ("at least " if r["last90_is_lower_bound"] else "", r["last90"]))
            lines.append("top 15 sources in the last 90 minutes:")
            for src, count in top_sources(r.get("recent", [])):
                lines.append("  %4d  %s" % (count, src))
            present = [k for k in KEY_SOURCES if any(k.lower() in str(a.get("source", "")).lower()
                                                     for a in r.get("recent", []))]
            lines.append("key sources present in the last 90 minutes: %s" % (", ".join(present) or "none"))
        lines.append("")
    base = results.get("none")
    for name in ("p30", "p50"):
        r = results.get(name)
        if r is not None and base is not None:
            lines.append("%s accepted by the rule's test: %s" % (name, "yes" if accepted(r, base) else "no"))
    lines.append("DECISION: %s" % ("endSourceRankPercentile %d" % decision if decision else "no filter"))
    lines.append("requests used by this probe: %d (limit %d)" % (used_by_probe, MAX_PROBE_REQUESTS))
    lines.append("lifetime feed B requests now: %d of %d" % (lifetime, news_b.LIFETIME_CAP))
    return "\n".join(lines) + "\n"


def run(store, session=None, poster=news_b.post, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    key = news_b.get_key()
    if not key:
        news_b.say("NO KEY: secret NEWSAPI_AI_KEY is not set")
        return 0
    store.ensure_tabs()
    state = store.get_state()

    def persist(n: int) -> None:
        state["requests_used"] = str(n)
        store.set_state(state)

    budget = news_b.Budget(int(float(state.get("requests_used") or 0)), persist, per_run=MAX_PROBE_REQUESTS)
    results: Dict[str, dict] = {}
    results["none"] = probe_variant("none", None, key, budget, now, session, poster)
    if results["none"]["ok"]:
        results["p30"] = probe_variant("p30", 30, key, budget, now, session, poster)
        if accepted(results["p30"], results["none"]):
            results["p50"] = probe_variant("p50", 50, key, budget, now, session, poster)
    decision = choose(results)
    text = report(results, decision, now, budget.run_used, budget.used)
    for line in text.splitlines():
        news_b.say(line)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(news_b.redact(text))
    return 0


def main() -> int:
    from src import feed_b
    try:
        return run(feed_b.SheetStore())
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        news_b.say("probe failed, exit 0: %s: %s" % (type(exc).__name__, str(exc)[:300]))
        return 0


if __name__ == "__main__":
    sys.exit(main())
