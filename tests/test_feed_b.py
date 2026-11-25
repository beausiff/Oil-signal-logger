"""Feed B (NewsAPI.ai twin bots): no network, no sheet, real code."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import build_page, feed_b_page, feed_b_probe, leaderboard, leaderboard_b
from src import feed_b, news_b, rules, scorer
from src.feed_b import SIGNALS_B, HEADLINES_B

FAKE_KEY = "FAKEKEY-9f3a1c77-DO-NOT-LEAK"
UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def main_row(year, month, day, hour, minute=5, price=100.0, version="v2"):
    local = datetime(year, month, day, hour, minute, tzinfo=rules.CHICAGO)
    return {
        "run_utc": local.astimezone(UTC).isoformat(), "run_chicago": local.isoformat(),
        "rules_version": version, "market_open": "TRUE", "brent_price": str(price),
        "price_time_utc": local.astimezone(UTC).isoformat(), "score": "3", "confidence": "high",
        "reasoning": "main", "score_source": "live",
    }


class MemoryStore:
    """The same interface as feed_b.SheetStore, in memory."""

    def __init__(self, main_rows, b_rows=None, state=None):
        self.main = list(main_rows)
        self.tabs = {SIGNALS_B: list(b_rows or []), HEADLINES_B: []}
        self.state = {k: "" for k in feed_b.STATE_KEYS}
        self.state.update(state or {})
        self.tables = {}
        self.state_writes = []

    def main_rows(self):
        return [dict(r) for r in self.main]

    def rows(self, tab):
        return [dict(r) for r in self.tabs.get(tab, [])]

    def ensure_tabs(self):
        pass

    def append(self, tab, rows):
        self.tabs.setdefault(tab, []).extend(dict(r) for r in rows)

    def known_hashes(self, count):
        return [r["hash"] for r in self.tabs[HEADLINES_B]][-count:]

    def get_state(self):
        return dict(self.state)

    def set_state(self, state):
        self.state = {k: ("" if state.get(k) is None else state.get(k, "")) for k in feed_b.STATE_KEYS}
        self.state_writes.append(dict(self.state))

    def write_table(self, tab, rows):
        self.tables[tab] = rows


@pytest.fixture(autouse=True)
def locked_filter(monkeypatch):
    monkeypatch.setattr(news_b, "SOURCE_FILTER_LOCKED", True)
    monkeypatch.setattr(news_b, "SOURCE_RANK_END_PERCENTILE", 30)
    monkeypatch.setenv("NEWSAPI_AI_KEY", FAKE_KEY)


def article(title, pub, index=None, source="Reuters"):
    return {"title": title, "url": "https://x/" + title.replace(" ", "-"),
            "dateTime": index or pub, "dateTimePub": pub, "source": {"title": source, "uri": "x.com"}}


def api_page(articles, pages=5, total=500):
    return {"articles": {"results": articles, "totalResults": total, "pages": pages}}


class Poster:
    """Stands in for news_b.post. Records bodies, never touches the network."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.bodies = []

    def __call__(self, body, session=None):
        self.bodies.append(body)
        if not self.pages:
            return None, "no more pages"
        page = self.pages.pop(0)
        return (page, "") if isinstance(page, dict) else (None, page)


def good_score(headlines, recent, window_minutes=None):
    return (scorer.Score(score=7, confidence="high", direction="escalation",
                         key_headlines=["a"], new_information=True, reasoning="r"),
            SimpleNamespace(as_note=lambda: "tokens_in=1", error=""))


NOW = dt("2026-11-24T16:30:00")


def latest_main(store_rows=None):
    return main_row(2026, 11, 24, 10, 5)     # 16:05 UTC (Chicago is UTC-6 in November)


# ---------------------------------------------------------- matching ----

def test_latest_unmatched_main_row_is_picked_and_older_unmatched_are_left():
    rows = [main_row(2026, 11, 24, 8), main_row(2026, 11, 24, 9), main_row(2026, 11, 24, 10)]
    b = [dict(rows[0])]
    target = feed_b.pick_target(rows, b, None)
    assert target["run_utc"] == rows[2]["run_utc"]
    # Once the latest is matched there is nothing to do: row 1 is NOT backfilled.
    assert feed_b.pick_target(rows, [dict(rows[0]), dict(rows[2])], None) is None


def test_v1_rows_and_rows_before_the_start_are_ignored():
    old = main_row(2026, 11, 24, 8, version="v1.1")
    early = main_row(2026, 11, 24, 9)
    late = main_row(2026, 11, 24, 10)
    assert feed_b.pick_target([old], [], None) is None
    start = dt(late["run_utc"])
    assert feed_b.pick_target([old, early], [], start) is None
    assert feed_b.pick_target([old, early, late], [], start)["run_utc"] == late["run_utc"]


def test_no_work_spends_nothing_and_writes_nothing():
    row = latest_main()
    store = MemoryStore([row], b_rows=[dict(row)])
    poster = Poster()
    assert feed_b.run_once(store, NOW, poster=poster, score_fn=good_score) == "no_work"
    assert poster.bodies == [] and store.state_writes == [] and len(store.tabs[SIGNALS_B]) == 1


def test_stale_main_row_is_left_unmatched():
    store = MemoryStore([latest_main()])
    poster = Poster()
    later = NOW + timedelta(minutes=feed_b.MAX_ROW_AGE_MINUTES + 5)
    assert feed_b.run_once(store, later, poster=poster, score_fn=good_score) == "stale"
    assert poster.bodies == []


def test_refuses_to_run_before_the_filter_is_locked(monkeypatch):
    monkeypatch.setattr(news_b, "SOURCE_FILTER_LOCKED", False)
    store = MemoryStore([latest_main()])
    poster = Poster()
    assert feed_b.run_once(store, NOW, poster=poster, score_fn=good_score) == "filter_not_locked"
    assert poster.bodies == [] and store.tabs[SIGNALS_B] == []


# ------------------------------------------------------------ window ----

def test_window_rule_matches_the_live_rule():
    t = dt("2026-11-24T16:05:00")
    assert feed_b.window_for(t, []) == 90                                              # first run
    assert feed_b.window_for(t, [{"run_utc": "2026-11-24T13:15:00+00:00"}]) == 180     # gap 170 + 10
    assert feed_b.window_for(t, [{"run_utc": "2026-11-24T15:35:00+00:00"}]) == 90      # gap 30, clamped up
    assert feed_b.window_for(t, [{"run_utc": "2026-11-23T01:00:00+00:00"}]) == 720     # clamped down


# ------------------------------------------------------------ paging ----

def budget(used=0, cap=news_b.LIFETIME_CAP, per_run=2, log=None):
    return news_b.Budget(used, (lambda n: log.append(n)) if log is not None else (lambda n: None),
                         cap=cap, per_run=per_run)


START, END = dt("2026-11-24T14:30:00"), dt("2026-11-24T16:05:00")


def test_paging_stops_when_a_page_reaches_back_past_the_window_start():
    page1 = api_page([article("a", "2026-11-24T16:00:00Z"), article("b", "2026-11-24T14:00:00Z")])
    poster = Poster(page1, api_page([article("never", "2026-11-24T10:00:00Z")]))
    res = news_b.fetch_window(START, END, FAKE_KEY, budget(), poster=poster)
    assert res.requests == 1 and not res.cut_short and not res.error
    assert len(poster.bodies) == 1


def test_stop_rule_uses_index_time_not_publish_time():
    # Published long ago (stray) but indexed just now: must NOT stop the paging.
    page1 = api_page([article("stray", "2019-01-01T00:00:00Z", index="2026-11-24T16:01:00Z")])
    page2 = api_page([article("old", "2026-11-24T14:00:00Z")])
    poster = Poster(page1, page2)
    res = news_b.fetch_window(START, END, FAKE_KEY, budget(), poster=poster)
    assert res.requests == 2 and not res.cut_short


def test_paging_stops_after_two_requests_and_says_the_window_was_cut_short():
    pages = [api_page([article("n%d" % i, "2026-11-24T16:0%d:00Z" % i)]) for i in range(5)]
    poster = Poster(*pages)
    b = budget()
    res = news_b.fetch_window(START, END, FAKE_KEY, b, poster=poster)
    assert res.requests == 2 and res.cut_short and len(poster.bodies) == 2
    assert b.used == 2


def test_body_has_the_agreed_fields_and_the_key_only_in_the_body():
    poster = Poster(api_page([]))
    news_b.fetch_window(START, END, FAKE_KEY, budget(), poster=poster, end_percentile=30)
    body = poster.bodies[0]
    assert body["keyword"] == feed_b.config.SEARCH_TERMS and body["keywordLoc"] == "title"
    assert body["lang"] == "eng" and body["isDuplicateFilter"] == "skipDuplicates"
    assert body["articlesSortBy"] == "date" and body["articlesSortByAsc"] is False
    assert (body["startSourceRankPercentile"], body["endSourceRankPercentile"]) == (0, 30)
    assert body["dateStart"] == body["dateEnd"] == "2026-11-24"
    assert body["apiKey"] == FAKE_KEY
    no_filter = news_b.build_body(FAKE_KEY, START.date(), END.date(), 1, None)
    assert "endSourceRankPercentile" not in no_filter


def test_window_touching_two_utc_days_queries_both():
    body = news_b.build_body(FAKE_KEY, dt("2026-11-23T23:00:00").date(), dt("2026-11-24T00:30:00").date(), 1)
    assert (body["dateStart"], body["dateEnd"]) == ("2026-11-23", "2026-11-24")


def test_only_articles_published_inside_the_window_survive_then_cap_at_40():
    arts = [article("in %d" % i, "2026-11-24T15:%02d:00Z" % (i % 60)) for i in range(55)]
    arts += [article("too late", "2026-11-24T16:30:00Z"), article("too early", "2026-11-24T14:00:00Z"),
             article("indexed late published in window", "2026-11-24T15:00:00Z", index="2026-11-24T17:00:00Z",
                     source="Other")]
    heads = news_b.window_headlines([news_b.slim(a) for a in arts], START, END, [], 95)
    titles = {h.title for h in heads}
    assert len(heads) == 40
    assert "too late" not in titles and "too early" not in titles
    assert all(START <= dt(h.published_utc[:19]) <= END for h in heads)


def test_known_hashes_are_deduped_like_the_live_path():
    a = news_b.slim(article("Same story", "2026-11-24T15:00:00Z"))
    first = news_b.window_headlines([a], START, END, [], 95)
    assert len(first) == 1
    assert news_b.window_headlines([a, a], START, END, [first[0].hash], 95) == []


# --------------------------------------------------------- lifetime cap ----

def test_lifetime_cap_refuses_before_any_request():
    log = []
    poster = Poster(api_page([]))
    res = news_b.fetch_window(START, END, FAKE_KEY, budget(used=1500, log=log), poster=poster)
    assert res.cap_reached and res.error and poster.bodies == [] and log == []


def test_counter_is_persisted_before_each_request():
    order = []
    b = news_b.Budget(10, lambda n: order.append(("persist", n)))

    def poster(body, session=None):
        order.append(("request", None))
        return api_page([article("a", "2026-11-24T10:00:00Z", index="2026-11-24T10:00:00Z")]), ""

    news_b.fetch_window(START, END, FAKE_KEY, b, poster=poster)
    assert order[:2] == [("persist", 11), ("request", None)]


def test_a_counter_that_cannot_be_saved_means_no_request():
    def broken(n):
        raise RuntimeError("sheet down")
    poster = Poster(api_page([]))
    res = news_b.fetch_window(START, END, FAKE_KEY, news_b.Budget(0, broken), poster=poster)
    assert res.error.startswith("counter_not_saved") and poster.bodies == []


def test_the_1501st_request_is_refused_in_a_full_run_and_a_blank_row_is_written():
    store = MemoryStore([latest_main()], state={"requests_used": "1500", "start_utc": "2026-11-24T10:00:00+00:00"})
    poster = Poster(api_page([]))
    assert feed_b.run_once(store, NOW, poster=poster, score_fn=good_score) == "wrote"
    row = store.tabs[SIGNALS_B][0]
    assert poster.bodies == [] and row["score"] == "" and "cap_reached" in row["notes"]
    assert store.state["requests_used"] == "1500"
    assert row["action"] == "error" and row["feed"] == "B"


# ------------------------------------------------------------- the run ----

def test_full_run_writes_one_row_copies_prices_and_updates_state():
    main = latest_main()
    store = MemoryStore([main])
    poster = Poster(api_page([article("Oil surges on Hormuz fears", "2026-11-24T16:00:00Z"),
                              article("Brent jumps", "2026-11-24T15:50:00Z", source="CNBC")], pages=1))
    assert feed_b.run_once(store, NOW, poster=poster, score_fn=good_score) == "wrote"
    rows = store.tabs[SIGNALS_B]
    assert len(rows) == 1
    row = rows[0]
    for col in ("run_utc", "run_chicago", "market_open", "brent_price", "price_time_utc"):
        assert row[col] == main[col]
    assert row["rules_version"] == "v2" and row["feed"] == "B" and row["score"] == 7
    assert row["action"] == "buy" and row["position_after"] == "long"
    assert row["new_headline_count"] == 2 and "feed=B newsapi.ai" in row["notes"]
    assert len(store.tabs[HEADLINES_B]) == 2
    assert store.state["start_utc"] == main["run_utc"] and store.state["requests_used"] == "1"
    assert store.state["position"] == "long" and store.state["last_main_run_utc"] == main["run_utc"]
    assert "leaderboard_b" in store.tables
    # A second run on the same main row has nothing to do.
    assert feed_b.run_once(store, NOW, poster=Poster(), score_fn=good_score) == "no_work"


def test_context_for_the_scorer_is_feed_bs_own_history_not_the_main_rows():
    seen = {}

    def spy(headlines, recent, window_minutes=None):
        seen["recent"], seen["window"] = list(recent), window_minutes
        return good_score(headlines, recent, window_minutes)

    first, second = main_row(2026, 11, 24, 9), main_row(2026, 11, 24, 10)
    b_prev = dict(first, reasoning="B history", feed="B", score="2")
    store = MemoryStore([first, second], b_rows=[b_prev],
                        state={"last_main_run_utc": first["run_utc"], "start_utc": first["run_utc"]})
    poster = Poster(api_page([article("x y", "2026-11-24T16:00:00Z")], pages=1))
    feed_b.run_once(store, NOW, poster=poster, score_fn=spy)
    assert [r["reasoning"] for r in seen["recent"]] == ["B history"]
    assert seen["window"] == 90


def test_api_error_writes_a_blank_score_row_with_a_note_and_the_run_returns():
    store = MemoryStore([latest_main()])
    poster = Poster("status 500 body boom")
    assert feed_b.run_once(store, NOW, poster=poster, score_fn=good_score) == "wrote"
    row = store.tabs[SIGNALS_B][0]
    assert row["score"] == "" and row["confidence"] == "" and row["action"] == "error"
    assert "newsapi_error" in row["notes"] and row["score_source"] == ""
    assert store.tabs[HEADLINES_B] == []


def test_missing_key_is_a_blank_row_too(monkeypatch):
    monkeypatch.delenv("NEWSAPI_AI_KEY")
    store = MemoryStore([latest_main()])
    feed_b.run_once(store, NOW, poster=Poster(), score_fn=good_score)
    row = store.tabs[SIGNALS_B][0]
    assert row["score"] == "" and "newsapi_no_key" in row["notes"]


def test_no_news_carries_the_previous_b_score():
    first, second = main_row(2026, 11, 24, 9), main_row(2026, 11, 24, 10)
    b_prev = dict(first, feed="B", score="4", confidence="medium", direction="neutral")
    store = MemoryStore([first, second], b_rows=[b_prev],
                        state={"last_main_run_utc": first["run_utc"], "start_utc": first["run_utc"]})
    feed_b.run_once(store, NOW, poster=Poster(api_page([], pages=1)), score_fn=good_score)
    row = store.tabs[SIGNALS_B][-1]
    assert row["score"] == 4 and row["score_source"] == "carried"


def test_position_is_rebuilt_from_rows_when_the_state_is_behind():
    first = main_row(2026, 11, 24, 9)
    b_prev = dict(first, feed="B", score="7", confidence="high", rules_version="v2")
    pos, note = feed_b.position_for({"last_main_run_utc": ""}, [b_prev])
    assert note == "state_rebuilt_from_rows" and pos.side == "long"
    pos, note = feed_b.position_for({"position": "long", "entry_price": "100",
                                     "last_main_run_utc": first["run_utc"]}, [b_prev])
    assert note == "" and pos.side == "long"


# ------------------------------------------------------- key never leaks ----

def test_the_key_never_appears_in_output_rows_state_or_notes(capsys):
    class Session:
        def post(self, url, json=None, timeout=None):
            raise RuntimeError("connection failed for body %s" % FAKE_KEY)

    store = MemoryStore([latest_main()])
    feed_b.run_once(store, NOW, session=Session(), score_fn=good_score)

    class Echo:
        def post(self, url, json=None, timeout=None):
            return SimpleNamespace(status_code=401, text="bad key %s" % FAKE_KEY, json=lambda: {})

    store2 = MemoryStore([latest_main()])
    feed_b.run_once(store2, NOW, session=Echo(), score_fn=good_score)
    out = capsys.readouterr()
    blob = out.out + out.err + json.dumps(store.tabs, default=str) + json.dumps(store.state_writes) \
        + json.dumps(store2.tabs, default=str) + json.dumps(store2.tables, default=str)
    assert FAKE_KEY not in blob
    assert "newsapi_error" in store.tabs[SIGNALS_B][0]["notes"]
    assert news_b.redact("x %s y" % FAKE_KEY) == "x *** y"


def test_main_never_raises_and_prints_a_redacted_reason(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("explode with %s" % FAKE_KEY)
    monkeypatch.setattr(feed_b, "SheetStore", boom)
    assert feed_b.main() == 0
    out = capsys.readouterr().out
    assert "explode" in out and FAKE_KEY not in out


def test_feed_b_never_calls_the_broker(monkeypatch):
    from src import broker

    def forbidden(*a, **k):
        raise AssertionError("feed B must not touch the broker")
    for name in dir(broker):
        if callable(getattr(broker, name)) and not name.startswith("_") and name[0].islower():
            monkeypatch.setattr(broker, name, forbidden)
    store = MemoryStore([latest_main()])
    poster = Poster(api_page([article("Oil surges", "2026-11-24T16:00:00Z")], pages=1))
    assert feed_b.run_once(store, NOW, poster=poster, score_fn=good_score) == "wrote"


# ------------------------------------------------------- leaderboard_b ----

def test_labels_are_relabelled_to_bot_nb():
    assert leaderboard_b.relabel("Bot 1: Original") == "Bot 1B: Original"
    assert leaderboard_b.relabel("Bot 6: Opposite") == "Bot 6B: Opposite"
    assert leaderboard_b.relabel("Bot 4: Lower bar + hold longer") == "Bot 4B: Lower bar + hold longer"
    rows = [dict(main_row(2026, 11, 24, 10), score="7")]
    table = leaderboard_b.standings_b(rows)
    assert [b["label"] for b in table["bots"]] == [
        "Bot %dB: %s" % (i, b["label"].split(": ", 1)[1]) for i, b in enumerate(leaderboard.BOTS, 1)]
    # The shared BOTS list is untouched.
    assert leaderboard.BOTS[0]["label"] == "Bot 1: Original"
    assert not any(str(n) in b["label"] for b in table["bots"] for n in range(7, 13))


def test_paired_comparison_uses_only_the_common_period():
    base = datetime(2026, 11, 24, 10, 0, tzinfo=rules.CHICAGO)

    def mk(hour, price, score, version="v2"):
        local = base.replace(hour=hour)
        return {"run_utc": local.astimezone(UTC).isoformat(), "run_chicago": local.isoformat(),
                "rules_version": version, "market_open": "TRUE", "brent_price": str(price),
                "score": str(score), "confidence": "high"}

    # Main saw hours 8 to 13; feed B only 10 to 13. Hour 8 would open a long at 90 in the main bot.
    main_rows = [mk(8, 90, 7), mk(9, 95, 1), mk(10, 100, 7), mk(11, 110, 1), mk(12, 120, 1)]
    b_rows = [mk(10, 100, 7), mk(11, 110, 1), mk(12, 120, 1)]
    p = leaderboard_b.paired(main_rows, b_rows)
    assert p["common_rows"] == 3 and len(p["pairs"]) == 6
    original = p["pairs"][0]
    assert original["main_balance"] == original["b_balance"] and original["diff"] == 0
    # Over its own whole history the main bot entered at 90, a different number to the paired one.
    whole_main = {b["key"]: b for b in leaderboard.standings(leaderboard.race_rows(main_rows), False)["bots"]}
    assert whole_main["original"]["balance_now"] != original["main_balance"]
    assert original["main_balance"] > 100
    # A row only the B feed has is excluded from both sides.
    b_rows.append(mk(13, 50, 7))
    assert leaderboard_b.paired(main_rows, b_rows)["common_rows"] == 3


def test_paired_has_no_pairs_without_overlap_and_tab_rows_still_render():
    table = leaderboard_b.build([main_row(2026, 11, 24, 10)], [])
    assert table["started"] is False and table["paired"]["pairs"] == []
    rows = leaderboard_b.tab_rows(table)
    assert rows[1][1] == "feed B has not started"
    table = leaderboard_b.build([main_row(2026, 11, 24, 10)], [dict(main_row(2026, 11, 24, 10), feed="B")])
    rows = leaderboard_b.tab_rows(table)
    assert any(r and r[0] == "1 vs 1B" for r in rows) and any(r and r[0] == "common_rows" for r in rows)
    assert all(len(r) <= 14 for r in rows) and len(rows) <= 60


def test_json_is_written(tmp_path):
    table = leaderboard_b.build([main_row(2026, 11, 24, 10)], [dict(main_row(2026, 11, 24, 10), feed="B")])
    path = leaderboard_b.write_json(table, str(tmp_path))
    assert json.load(open(path))["paired"]["common_rows"] == 1


# --------------------------------------------------------------- page ----

def test_page_renders_with_zero_feed_b_rows():
    main = [main_row(2026, 11, 24, 10)]
    page = build_page.build(main)
    assert 'id="feedb"' in page and "Feed B: same six bots on NewsAPI.ai" in page
    assert "has not logged a row yet" in page
    page2 = build_page.build(main, rows_b=[])
    assert page == page2
    for banned in ("—", "–"):
        assert banned not in page


def test_page_renders_with_a_few_feed_b_rows():
    main = [main_row(2026, 11, 24, 10), main_row(2026, 11, 24, 11, price=101.0), main_row(2026, 11, 24, 12, price=103.0)]
    b = [dict(main[1], feed="B", score="7"), dict(main[2], feed="B", score="1")]
    page = build_page.build(main, rows_b=b)
    section = page[page.index('<section id="feedb">'):]
    section = section[:section.index("</section>")]
    assert "Bot 1B: Original" in section and "Bot 6B: Opposite" in section
    assert section.count("$100 is now") >= 2
    assert "1 vs 1B" in section and "6 vs 6B" in section
    assert "not the overall ranking" in section and "started later" in section
    assert "24 Nov 2026" in section
    assert page.count("<script") == 1


def test_page_with_one_b_row_and_no_overlap_says_so():
    main = [main_row(2026, 11, 24, 10)]
    b = [dict(main_row(2026, 11, 24, 12), feed="B", score="7")]
    section = feed_b_page.section_html(main, b)
    assert "No hour has been logged by both feeds yet." in section


# ----------------------------------------------------------- the probe ----

def probe_result(name, pct, ok, total, last90):
    return {"name": name, "percentile": pct, "ok": ok, "error": "" if ok else "bad", "total": total,
            "requests": 1, "articles": [], "last90": last90, "last90_is_lower_bound": False, "recent": []}


def test_filter_rule_picks_the_tightest_accepted_filter_with_enough_articles():
    base = probe_result("none", None, True, 1000, 120)
    pick = feed_b_probe.choose
    assert pick({"none": base, "p30": probe_result("p30", 30, True, 400, 40)}) == 30
    assert pick({"none": base, "p30": probe_result("p30", 30, True, 400, 14),
                 "p50": probe_result("p50", 50, True, 600, 60)}) == 50
    assert pick({"none": base, "p30": probe_result("p30", 30, True, 1000, 120)}) is None   # ignored field
    assert pick({"none": base, "p30": probe_result("p30", 30, False, None, 0)}) is None   # rejected
    assert pick({"none": base, "p30": probe_result("p30", 30, True, 400, 5),
                 "p50": probe_result("p50", 50, True, 600, 14)}) is None


def test_probe_spends_at_most_12_requests_and_counts_them(tmp_path, monkeypatch):
    monkeypatch.setattr(feed_b_probe, "OUT_PATH", str(tmp_path / "probe.txt"))
    now = dt("2026-11-24T16:00:00")
    full = [article("t%d" % i, "2026-11-24T15:%02d:00Z" % (59 - i % 60)) for i in range(100)]

    def poster(body, session=None):
        total = 1000 if "endSourceRankPercentile" not in body else 400
        return api_page(full, pages=9, total=total), ""

    store = MemoryStore([])
    store.tabs["feed_b_state"] = []
    assert feed_b_probe.run(store, poster=poster, now=now) == 0
    assert int(store.state["requests_used"]) <= 12
    text = open(tmp_path / "probe.txt").read()
    assert "DECISION:" in text and FAKE_KEY not in text


# ------------------------------------------------------------ workflow ----

def test_workflow_is_isolated_from_the_hourly_group():
    text = open(".github/workflows/feed_b.yml").read()
    assert "group: oil-feed-b" in text and "cancel-in-progress: false" in text
    assert "oil-signal-logger" not in text.replace("# Its own queue. Never the hourly bot's group (oil-signal-logger).", "")
    assert "workflow_run:" in text and 'workflows: ["hourly"]' in text and "types: [completed]" in text
    assert "timeout-minutes: 10" in text and "NEWSAPI_AI_KEY: ${{ secrets.NEWSAPI_AI_KEY }}" in text
    assert "OANDA" not in text
    hourly = open(".github/workflows/hourly.yml").read()
    assert "feed_b" not in hourly
