import csv
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from scripts import pilot_newsapi_vs_live as p
from src import news

FAKE_KEY = "FAKEKEY-9f3a1c77-DO-NOT-LEAK"


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def sig(run, version="v1.1"):
    return {"run_utc": run, "rules_version": version}


# ---------------------------------------------------------------- windows ----

def test_window_v1_is_90_minutes_regardless_of_gap():
    sigs = [sig("2026-11-20T10:00:00+00:00"), sig("2026-11-20T16:00:00+00:00", "v1.1")]
    s, e, m = p.row_window(sigs, 1)
    assert m == 90 and e == dt("2026-11-20T16:00:00") and s == dt("2026-11-20T14:30:00")
    assert p.row_window(sigs, 0)[2] == 90


def test_window_v2_follows_live_rule():
    sigs = [sig("2026-11-20T10:00:00+00:00", "v2"),
            sig("2026-11-20T12:50:00+00:00", "v2"),     # gap 170 -> 180
            sig("2026-11-20T13:20:00+00:00", "v2"),     # gap 30 -> 40 -> clamp 90
            sig("2026-11-21T13:20:00+00:00", "v2")]     # gap 1440 -> clamp 720
    assert p.row_window(sigs, 0)[2] == 90               # no previous row
    assert p.row_window(sigs, 1)[2] == 180
    assert p.row_window(sigs, 2)[2] == 90
    s, e, m = p.row_window(sigs, 3)
    assert m == 720 and s == e - timedelta(minutes=720)


def test_utc_days_and_needs():
    s, e = dt("2026-11-20T23:00:00"), dt("2026-11-21T00:30:00")
    assert p.utc_days(s, e) == [date(2026, 11, 20), date(2026, 11, 21)]
    need = p.day_needs([(s, e), (dt("2026-11-20T12:00:00"), dt("2026-11-20T13:30:00"))])
    assert need[date(2026, 11, 20)] == dt("2026-11-20T12:00:00")
    assert need[date(2026, 11, 21)] == dt("2026-11-21T00:00:00")   # clipped to midnight


# ------------------------------------------------------- paging stop rule ----

def test_page_decision_stop_rule():
    need = dt("2026-11-20T12:00:00")
    older, newer = dt("2026-11-20T11:59:00"), dt("2026-11-20T15:00:00")
    assert p.page_decision(1, 5, 100, newer, need) == "more"
    assert p.page_decision(2, 5, 100, older, need) == "done"          # oldest older than need
    assert p.page_decision(5, 5, 40, newer, need) == "done"           # last page
    assert p.page_decision(1, 5, 0, None, need) == "done"             # empty page
    assert p.page_decision(6, 9, 100, newer, need) == "truncated"     # limit hit, more pages exist
    assert p.page_decision(6, 6, 100, newer, need) == "done"          # limit equals last page
    assert p.page_decision(6, 9, 100, older, need) == "done"          # reached need on the limit page


class FakeSession:
    """Serves day pages of 100 articles, newest first, one minute apart."""

    def __init__(self, total, key=FAKE_KEY):
        self.total, self.bodies = total, []

    def post(self, url, json=None, timeout=None):
        self.bodies.append(json)
        page = json["articlesPage"]
        day = datetime.fromisoformat(json["dateStart"]).replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
        arts = []
        for k in range((page - 1) * 100, min(page * 100, self.total)):
            t = (day - timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M:%SZ")
            arts.append({"title": "Oil story %d" % k, "url": "http://x/%d" % k, "dateTime": t,
                         "dateTimePub": t, "source": {"uri": "s.com", "title": "S"}})
        body = {"articles": {"results": arts, "totalResults": self.total, "pages": -(-self.total // 100),
                             "page": page}}
        return type("R", (), {"status_code": 200, "text": "x", "json": lambda self: body})()


def test_fetch_day_truncates_at_page_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    cap = p.RequestCap(str(tmp_path / "r.txt"), 300)
    sess = FakeSession(total=2000)
    res = p.fetch_day(date(2026, 11, 20), dt("2026-11-20T00:00:00"), FAKE_KEY, cap, session=sess)
    assert res["meta"]["truncated"] is True and res["meta"]["pages_fetched"] == 6
    assert len(res["articles"]) == 600 and cap.used == 6 and res["meta"]["total_results"] == 2000
    assert sess.bodies[0]["apiKey"] == FAKE_KEY and sess.bodies[0]["keywordOper"] == "or"
    assert "Red Sea" in sess.bodies[0]["keyword"]


def test_fetch_day_stops_when_need_reached(tmp_path, monkeypatch):
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    cap = p.RequestCap(str(tmp_path / "r.txt"), 300)
    # articles run from 23:59 backwards one per minute; page 1 covers 23:59..22:20
    res = p.fetch_day(date(2026, 11, 20), dt("2026-11-20T22:00:00"), FAKE_KEY, cap, session=FakeSession(2000))
    assert res["meta"]["pages_fetched"] == 2 and res["meta"]["truncated"] is False
    # short day: ends on the last page
    cap2 = p.RequestCap(str(tmp_path / "r2.txt"), 300)
    res = p.fetch_day(date(2026, 11, 20), dt("2026-11-20T00:00:00"), FAKE_KEY, cap2, session=FakeSession(230))
    assert res["meta"]["pages_fetched"] == 3 and res["meta"]["truncated"] is False and len(res["articles"]) == 230


def test_day_store_uses_cache_and_never_refetches(tmp_path, monkeypatch):
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    cache = str(tmp_path / "cache")
    cap = p.RequestCap(str(tmp_path / "r.txt"), 300)
    day = date(2026, 11, 20)
    needs = {day: dt("2026-11-20T22:00:00")}
    sess = FakeSession(2000)
    store = p.DayStore(needs, FAKE_KEY, cap, session=sess, cache_dir=cache)
    a = store.get(day)
    used = cap.used
    store.get(day)
    assert cap.used == used
    store2 = p.DayStore(needs, FAKE_KEY, cap, session=sess, cache_dir=cache)   # a rerun
    b = store2.get(day)
    assert cap.used == used and b["meta"] == a["meta"] and len(b["articles"]) == len(a["articles"])
    row = json.loads(open(p.cache_path(day, cache)).read().splitlines()[1])
    assert sorted(row) == ["dateTime", "dateTimePub", "source", "title", "url"]


def test_coverage_flags():
    day = {"meta": {"day": "2026-11-20", "truncated": True, "floor_pub": "2026-11-20T14:00:00+00:00"}, "articles": []}
    assert p.coverage([day], dt("2026-11-20T13:00:00")) == (True, True)
    assert p.coverage([day], dt("2026-11-20T15:00:00")) == (True, False)
    ok = {"meta": {"day": "2026-11-20", "truncated": False, "floor_pub": ""}, "articles": []}
    assert p.coverage([ok], dt("2026-11-20T00:00:00")) == (False, False)


# ------------------------------------------------------------ slicing --------

def test_art_time_prefers_pub_with_fallback():
    a = {"dateTimePub": "2026-11-20T10:00:00Z", "dateTime": "2026-11-20T10:05:00Z"}
    assert p.art_time(a) == (dt("2026-11-20T10:00:00"), "dateTimePub")
    assert p.art_time({"dateTime": "2026-11-20T10:05:00Z"}) == (dt("2026-11-20T10:05:00"), "dateTime")
    assert p.art_time({"dateTimePub": "", "dateTime": ""})[0] is None


def test_window_slice_normalise_dedupe_cap():
    run = dt("2026-11-20T12:00:00")
    arts = [{"title": "Oil story %d - Reuters" % i, "source": "Reuters", "url": "",
             "dateTimePub": (run - timedelta(minutes=i)).isoformat()} for i in range(1, 61)]
    arts.append(dict(arts[0]))                                                     # duplicate
    arts.append({"title": "Old", "source": "X", "dateTimePub": (run - timedelta(minutes=500)).isoformat()})
    inside = p.window_articles(arts, run - timedelta(minutes=90), run)
    assert len(inside) == 61 and all("Reuters" not in h.title for h in inside)     # suffix stripped
    heads = p.final_headlines(inside, run, 90)
    assert len(heads) == 40 and heads[0].title == "Oil story 1"


# ------------------------------------------------------------- caps ----------

def test_request_cap_refuses_301st(tmp_path):
    path = str(tmp_path / "r.txt")
    cap = p.RequestCap(path, p.MAX_REQUESTS)
    for _ in range(300):
        cap.consume()
    with pytest.raises(p.RequestCapExceeded):
        cap.consume()
    assert open(path).read().strip() == "300"
    assert p.RequestCap(path, p.MAX_REQUESTS).remaining == 0


def test_api_post_does_not_post_past_cap(tmp_path):
    path = tmp_path / "r.txt"
    path.write_text("300\n")
    sess = FakeSession(10)
    with pytest.raises(p.RequestCapExceeded):
        p.api_post(p.build_body(FAKE_KEY, date(2026, 11, 20), 1), p.RequestCap(str(path), 300), session=sess)
    assert sess.bodies == []


def test_model_call_cap_refuses_61st(tmp_path):
    from scripts import pilot_archive_vs_live as old
    path = str(tmp_path / "c.txt")
    cap = old.SpendCap(path, p.MAX_CALLS)

    class Inner:
        class messages:
            @staticmethod
            def create(**kw):
                return "ok"

    client = old.CountingClient(Inner, cap)
    for _ in range(60):
        assert client.messages.create() == "ok"
    with pytest.raises(old.SpendCapExceeded):
        client.messages.create()
    assert p.MAX_CALLS == 60 and p.MAX_REQUESTS == 300


# ------------------------------------------------------------ verdict --------

def test_verdict_rule():
    assert p.verdict(0.60, 0.70, 30) == "GOOD ENOUGH"
    assert p.verdict(0.95, 0.69, 30) == "BORDERLINE"
    assert p.verdict(0.59, 0.99, 30) == "BORDERLINE"
    assert p.verdict(0.40, 0.10, 30) == "BORDERLINE"
    assert p.verdict(0.399, 0.99, 30) == "NOT GOOD ENOUGH"
    assert p.verdict(0.95, 0.99, 29).startswith("NO VERDICT")
    assert p.verdict(None, 0.0, 40).startswith("NO VERDICT")


# ------------------------------------------------- no key / key never leaks --

def _chdir_repo(tmp_path, monkeypatch):
    """Run in a scratch copy layout so nothing real is written."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    monkeypatch.chdir(tmp_path)
    os.makedirs("backup")
    for f in ("signals.csv", "headlines.csv"):
        with open(os.path.join(root, "backup", f), "rb") as src, open(os.path.join("backup", f), "wb") as dst:
            dst.write(src.read())


def test_no_key_exits_zero_and_spends_nothing(tmp_path, monkeypatch, capsys):
    _chdir_repo(tmp_path, monkeypatch)
    monkeypatch.delenv("NEWSAPI_AI_KEY", raising=False)

    def boom(*a, **k):
        raise AssertionError("network used without a key")

    monkeypatch.setattr("requests.Session.post", boom)
    monkeypatch.setattr("requests.post", boom)
    assert p.main([]) == 0
    assert open(p.TXT_PATH).read() == "NO KEY: secret NEWSAPI_AI_KEY is not set\n"
    assert not os.path.exists(p.REQUESTS_PATH) and not os.path.exists(p.CALLS_PATH)
    assert p.main(["--probe-only"]) == 0
    assert "NO KEY: secret NEWSAPI_AI_KEY is not set" in capsys.readouterr().out
    monkeypatch.setenv("NEWSAPI_AI_KEY", "   ")
    assert p.main([]) == 0 and open(p.TXT_PATH).read().startswith("NO KEY")


def test_key_never_printed_or_written(tmp_path, monkeypatch, capsys):
    _chdir_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("NEWSAPI_AI_KEY", FAKE_KEY)

    class Echo:
        """An API that fails and echoes the request body (key included) in its error text."""
        def post(self, url, json=None, timeout=None):
            return type("R", (), {"status_code": 401, "text": "bad request %s" % json, "json": lambda s: {}})()

    monkeypatch.setattr("requests.Session", lambda: Echo())
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    assert p.main(["--probe-only"]) == 0
    out = capsys.readouterr().out
    assert "status 401" in out and "bad request" in out
    assert FAKE_KEY not in out
    for root, _, files in os.walk(tmp_path):
        for f in files:
            if f.endswith(".csv") and root.endswith("backup"):
                continue
            assert FAKE_KEY not in open(os.path.join(root, f), errors="ignore").read()
    assert open(p.REQUESTS_PATH).read().strip() == "1"        # the probe is one counted request


def test_probe_prints_shape_without_key(tmp_path, monkeypatch, capsys):
    _chdir_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("NEWSAPI_AI_KEY", FAKE_KEY)
    monkeypatch.setattr("requests.Session", lambda: FakeSession(250))
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    assert p.main(["--probe-only"]) == 0
    out = capsys.readouterr().out
    assert "top level keys: ['articles']" in out and "totalResults: 250" in out and "pages: 3" in out
    assert "dateTimePub" in out and FAKE_KEY not in out
    assert open(p.REQUESTS_PATH).read().strip() == "1"
    assert not os.path.exists(p.CALLS_PATH) and not os.path.isdir(p.CACHE_DIR) or not any(
        f.endswith(".jsonl") for f in os.listdir(p.CACHE_DIR))
    assert FAKE_KEY not in open(p.TXT_PATH).read()


def test_report_never_contains_key_and_handles_sparse_rows(monkeypatch):
    monkeypatch.setenv("NEWSAPI_AI_KEY", FAKE_KEY)
    rows = [{"run_utc": "2026-11-20T10:00:00+00:00", "status": "no_headlines", "truncated_day": 1, "coverage_gap": 1},
            {"run_utc": "2026-11-20T11:00:00+00:00", "status": "ok", "live_score": "1", "newsapi_score": "2",
             "live_confidence": "low", "newsapi_confidence": "low", "live_headlines": "13",
             "newsapi_headlines": "18", "newsapi_in_window": "25", "title_overlap": "", "cost_usd": "0.01",
             "top_live_titles": "", "top_newsapi_titles": "x " + FAKE_KEY}]
    day = {"meta": {"day": "2026-11-20", "total_results": 900, "articles": 600, "pages_fetched": 6,
                    "pages_total": 9, "truncated": True, "floor_pub": "2026-11-20T14:00:00+00:00"},
           "articles": [{"title": "t", "source": "Reuters", "url": "", "dateTime": "2026-11-20T10:05:00Z",
                         "dateTimePub": "2026-11-20T10:00:00Z"},
                        {"title": "u", "source": "Bloomberg", "url": "", "dateTime": "2026-11-20T00:00:00Z",
                         "dateTimePub": ""}]}
    art = p.article_stats([day])
    text = p.build_report(rows, 56, 7, 1, [day["meta"]], art, {"before_cap": 25.0})
    assert "rows with zero headlines in window: 1" in text
    assert "rows affected by truncated days" in text and "NewsAPI.ai requests used: 7 of 300" in text
    assert "model calls used: 1 of 60" in text and "VERDICT: NO VERDICT" in text
    assert "median lag dateTime minus dateTimePub: 5.0 min" in text
    assert "Reuters: present" in text and "CNBC: absent" in text
    assert "share with minute level publish times (not exactly midnight): 50.0%" in text
    assert FAKE_KEY not in text and "x ***" in text


def test_sample_is_deterministic_and_bounded():
    sigs = []
    t = dt("2026-11-01T00:00:00")
    for i in range(400):
        sigs.append({"run_utc": (t + timedelta(hours=2 * i)).isoformat(), "score": "1", "score_source": "live" if i % 2 else "",
                     "new_headline_count": "10", "rules_version": "v1.1"})
    sigs.append({"run_utc": "2026-11-24T00:00:00+00:00", "score": "", "score_source": "", "new_headline_count": "10"})
    anchor = date(2026, 11, 25)
    a, b = p.sample_rows(sigs, anchor), p.sample_rows(sigs, anchor)
    assert a == b and len(a) == p.MAX_ROWS
    assert all(r["run_utc"][:10] >= "2026-10-31" and r["score"] for r in a)
