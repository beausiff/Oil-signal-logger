from datetime import date, datetime, timedelta, timezone

import pytest

from scripts import pilot_archive_vs_live as old
from scripts import pilot_google_vs_live as g
from src import news


def H(title, pub, source="Src"):
    return news.Headline(hash=news.headline_hash(title, source), published_utc=pub, source=source,
                         title=title, url="")


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


RUN = dt("2026-10-05T12:00:00")


def test_slice_window_by_pubdate_inclusive():
    hs = [H("a", "2026-10-05T10:29:59+00:00"),   # just before the window
          H("b", "2026-10-05T10:30:00+00:00"),   # start, inside
          H("c", "2026-10-05T11:15:00+00:00"),
          H("d", "2026-10-05T12:00:00+00:00"),   # end, inside
          H("e", "2026-10-05T12:00:01+00:00"),   # after the run
          H("f", "2026-10-04T12:00:00+00:00")]
    out = g.slice_window(hs, RUN - timedelta(minutes=90), RUN)
    assert [h.title for h in out] == ["b", "c", "d"]


def test_window_headlines_dedupes_and_caps_newest_first():
    day = [H("t%d" % i, (RUN - timedelta(minutes=i)).isoformat()) for i in range(1, 61)]
    day.append(H("t1", (RUN - timedelta(minutes=1)).isoformat()))        # duplicate hash
    day.append(H("old", (RUN - timedelta(minutes=200)).isoformat()))     # outside window
    out = g.window_headlines([day], RUN)
    assert len(out) == 40
    assert out[0].title == "t1" and len({h.hash for h in out}) == 40
    assert all("old" != h.title for h in out)
    # two day feeds are merged
    a = [H("x", "2026-10-05T11:00:00+00:00")]
    b = [H("y", "2026-10-05T11:30:00+00:00")]
    assert [h.title for h in g.window_headlines([a, b], RUN)] == ["y", "x"]


def test_window_days_single_and_midnight_crossing():
    assert g.window_days(dt("2026-10-05T10:30:00"), RUN) == [date(2026, 10, 5)]
    run = dt("2026-09-28T00:50:50")
    assert g.window_days(run - timedelta(minutes=90), run) == [date(2026, 9, 27), date(2026, 9, 28)]


def test_dated_query_before_is_exclusive_upper_bound():
    q = g.dated_query(date(2026, 9, 30))
    assert q.endswith(" after:2026-09-30 before:2026-10-01")
    assert "when:" not in q
    assert q.startswith("(oil OR Brent OR crude")
    assert "q=" in g.dated_url(date(2026, 9, 30)) and "ceid=US%3Aen" in g.dated_url(date(2026, 9, 30))


def test_spend_cap_refuses_61st(tmp_path):
    path = str(tmp_path / "calls.txt")
    cap = old.SpendCap(path, g.MAX_CALLS)

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
    assert open(path).read().strip() == "60"
    assert old.SpendCap(path, g.MAX_CALLS).remaining == 0   # persists across runs


def test_verdict_rule():
    assert g.verdict(0.60, 0.70, 30) == "GOOD ENOUGH"
    assert g.verdict(0.95, 0.69, 30) == "BORDERLINE"
    assert g.verdict(0.59, 0.99, 30) == "BORDERLINE"
    assert g.verdict(0.40, 0.10, 30) == "BORDERLINE"
    assert g.verdict(0.399, 0.99, 30) == "NOT GOOD ENOUGH"
    assert g.verdict(None, 0.0, 0).startswith("NO VERDICT")


def test_depth_dates_are_weekdays_in_range():
    ds = g.depth_dates()
    assert len(ds) == 8
    assert all(d.weekday() < 5 and date(2026, 3, 2) <= d <= date(2026, 9, 15) for d in ds)
    assert ds == sorted(ds)


def test_day_cache_fetches_once():
    calls = []
    cache = g.DayCache(lambda d: calls.append(d) or ([], 0))
    cache.get(date(2026, 10, 1)); cache.get(date(2026, 10, 1)); cache.get(date(2026, 10, 2))
    assert calls == [date(2026, 10, 1), date(2026, 10, 2)]


def test_fetch_spacing_and_retries():
    slept, now = [], [1000.0]

    class Resp:
        def __init__(self, code): self.status_code, self.content = code, b"<rss><channel></channel></rss>"

    class Sess:
        n = 0
        def get(self, url, **kw):
            Sess.n += 1
            assert "Mozilla" in kw["headers"]["User-Agent"]
            return Resp(503 if Sess.n < 3 else 200)

    g._last_request[0] = 0.0
    res = g.fetch_day(date(2026, 10, 1), session=Sess(), sleep=lambda s: slept.append(s), clock=lambda: now[0])
    assert res == ([], 0) and Sess.n == 3
    assert slept == [5, 3.0, 15, 3.0]   # backoff 5 then 15, each followed by the >= 3s spacing wait

    class Dead:
        n = 0
        def get(self, url, **kw):
            Dead.n += 1
            return Resp(503)

    assert g.fetch_day(date(2026, 10, 1), session=Dead(), sleep=lambda s: None, clock=lambda: now[0]) is None
    assert Dead.n == 4   # first try + 3 retries
