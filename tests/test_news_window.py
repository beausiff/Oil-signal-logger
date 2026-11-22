"""v2: the news window covers everything since the previous run."""

from datetime import datetime, timedelta, timezone

from src import config, main, news, scorer

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def headline(title, age_minutes):
    return news.Headline(
        hash=news.headline_hash(title, "Reuters"),
        published_utc=(NOW - timedelta(minutes=age_minutes)).isoformat(),
        source="Reuters", title=title, url="https://example.com",
    )


def test_the_window_clamps():
    assert main.news_window_minutes(None) == 90
    assert main.news_window_minutes(30) == 90
    assert main.news_window_minutes(412) == 422
    assert main.news_window_minutes(3 * 24 * 60) == config.NEWS_LOOKBACK_MAX_MINUTES == 720


def test_a_five_hour_old_headline_needs_the_wide_window():
    old = headline("Pipeline attacked", 5 * 60)
    assert news.filter_and_dedupe([old], [], NOW) == []
    assert news.filter_and_dedupe([old], [], NOW, lookback_minutes=6 * 60) == [old]


def test_collect_passes_the_window_through(monkeypatch):
    old = headline("Pipeline attacked", 5 * 60)
    monkeypatch.setattr(news, "fetch_google_news", lambda *a, **k: [old])
    monkeypatch.setattr(news, "fetch_gnews", lambda *a, **k: [])
    assert news.collect([], NOW) == []
    assert news.collect([], NOW, lookback_minutes=400) == [old]


def test_over_the_cap_the_newest_are_kept():
    items = [headline("story %d" % i, i) for i in range(1, 60)]
    kept = news.filter_and_dedupe(items, [], NOW, lookback_minutes=720)
    assert len(kept) == config.MAX_HEADLINES_PER_RUN
    assert kept[0].title == "story 1"
    assert kept[-1].title == "story %d" % config.MAX_HEADLINES_PER_RUN


def test_the_user_message_states_the_window():
    message = scorer.build_user_message([], [], window_minutes=422)
    assert "422 minutes since the previous run" in message
    assert "last hour" not in message


def test_the_run_logs_the_window_it_used(monkeypatch):
    from tests.test_main import _FrozenDatetime, fake_headline, wire_news, wire_price
    from tests import test_main
    from src import sheets
    from tests.fakes import FakeSheets

    client = FakeSheets()
    monkeypatch.setattr(main.sheets, "open_client", lambda dry_run=False: client)
    monkeypatch.setattr(main, "datetime", _FrozenDatetime)
    _FrozenDatetime.now_value = datetime(2026, 11, 24, 17, 5, tzinfo=timezone.utc)
    client.state = dict(sheets.EMPTY_STATE,
                        last_run_utc=(_FrozenDatetime.now_value - timedelta(minutes=412)).isoformat())
    seen = {}
    monkeypatch.setattr(main.news, "collect",
                        lambda known, now, lookback_minutes=90: seen.setdefault("w", lookback_minutes) and [])
    wire_price(monkeypatch, 80.0)
    main.run()
    assert seen["w"] == 422
    assert "news_window_min=422" in client.records("signals")[-1]["notes"]
