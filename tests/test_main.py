"""End to end cover for one hourly run, with every external call faked."""

from datetime import datetime, timezone

import pytest

from src import main, news, price as price_client, rules, scorer, sheets
from src.sheets import HEADLINES, SIGNALS, TRADES
from tests.fakes import FakeSheets


def wednesday_utc(hour=17):
    # 17:05 UTC is 12:05 Chicago, inside the session.
    return datetime(2026, 9, 16, hour, 5, tzinfo=timezone.utc)


@pytest.fixture
def wired(monkeypatch):
    client = FakeSheets()
    monkeypatch.setattr(sheets, "open_client", lambda dry_run=False: client)
    monkeypatch.setattr(main.sheets, "open_client", lambda dry_run=False: client)
    monkeypatch.setattr(main, "datetime", _FrozenDatetime)
    _FrozenDatetime.now_value = wednesday_utc()
    return client


class _FrozenDatetime(datetime):
    now_value = wednesday_utc()

    @classmethod
    def now(cls, tz=None):
        return cls.now_value if tz is None else cls.now_value.astimezone(tz)


def fake_headline(title="Tanker struck in the Red Sea"):
    published = _FrozenDatetime.now_value.isoformat()
    return news.Headline(
        hash=news.headline_hash(title, "Reuters"),
        published_utc=published,
        source="Reuters",
        title=title,
        url="https://example.com/1",
    )


def wire_news(monkeypatch, headlines):
    monkeypatch.setattr(main.news, "collect", lambda known, now: list(headlines))


def wire_price(monkeypatch, value, note=""):
    quote = price_client.PriceQuote(price=value, price_time_utc="2026-09-16T17:00:00+00:00", note=note)
    monkeypatch.setattr(main.price_client, "fetch_brent", lambda *a, **k: quote)


def wire_score(monkeypatch, score, confidence="high"):
    result = scorer.Score(
        score=score, confidence=confidence, direction="escalation",
        key_headlines=["Tanker struck in the Red Sea"], new_information=True,
        reasoning="test",
    )
    usage = scorer.Usage(input_tokens=1000, output_tokens=50, attempts=1)
    monkeypatch.setattr(main.scorer, "score_news", lambda h, s, **k: (result, usage))


def test_strong_score_opens_a_paper_long(wired, monkeypatch):
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    wire_score(monkeypatch, 8)

    assert main.run() == 0

    row = wired.records(SIGNALS)[0]
    assert row["action"] == "buy"
    assert row["position_after"] == "long"
    assert row["market_open"] is True
    assert row["brent_price"] == 80.0
    assert row["rules_version"] == "v1"
    assert "tokens_in=1000" in row["notes"]

    assert wired.get_state()["position"] == "long"
    assert wired.get_state()["entry_price"] == 80.0
    assert len(wired.records(HEADLINES)) == 1
    assert wired.records(HEADLINES)[0]["run_utc"] == _FrozenDatetime.now_value.isoformat()


def test_reversal_closes_and_writes_a_trade(wired, monkeypatch):
    wired.state = {
        "position": "long", "entry_price": 80.0,
        "entry_utc": "2026-09-16T12:05:00+00:00", "entry_score": 7,
        "best_price": 80.0, "trade_id": "T-1", "last_run_utc": "",
    }
    wire_news(monkeypatch, [fake_headline("Ceasefire agreed")])
    wire_price(monkeypatch, 82.0)
    wire_score(monkeypatch, -5)

    main.run()

    trades = wired.records(TRADES)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "reversal"
    assert trades[0]["pnl_usd_bbl"] == pytest.approx(1.95)
    assert trades[0]["hours_held"] == pytest.approx(5.0)
    assert wired.get_state()["position"] == "flat"


def test_no_new_headlines_carries_the_last_score(wired, monkeypatch):
    wired.append(SIGNALS, [{
        "run_utc": "2026-09-16T16:05:00+00:00", "score": 4,
        "confidence": "medium", "direction": "escalation", "reasoning": "earlier",
    }])
    wire_news(monkeypatch, [])
    wire_price(monkeypatch, 80.0)

    main.run()

    row = wired.records(SIGNALS)[-1]
    assert row["score"] == 4
    assert "no_new_news" in row["notes"]
    assert row["action"] == "flat"
    assert row["new_headline_count"] == 0


def test_invalid_claude_json_logs_an_error_and_skips_trade_logic(wired, monkeypatch):
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    monkeypatch.setattr(
        main.scorer, "score_news",
        lambda h, s, **k: (None, scorer.Usage(input_tokens=900, output_tokens=40, attempts=2)),
    )

    assert main.run() == 0

    row = wired.records(SIGNALS)[0]
    assert row["action"] == "error"
    assert row["position_after"] == "flat"
    assert row["score"] == ""
    assert "scorer_invalid_json" in row["notes"]
    assert wired.records(TRADES) == []


def test_a_missing_price_does_not_stop_the_run(wired, monkeypatch):
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, None, note="price_fetch_failed:ConnectionError")
    wire_score(monkeypatch, 9)

    main.run()

    row = wired.records(SIGNALS)[0]
    assert row["brent_price"] == ""
    assert row["action"] == "flat"
    assert "price_fetch_failed" in row["notes"]


def test_weekend_run_logs_news_but_never_trades(wired, monkeypatch):
    _FrozenDatetime.now_value = datetime(2026, 9, 19, 14, 5, tzinfo=timezone.utc)  # Saturday
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    wire_score(monkeypatch, 10)

    main.run()

    row = wired.records(SIGNALS)[0]
    assert row["market_open"] is False
    assert row["action"] == "flat"
    assert row["position_after"] == "flat"
    assert len(wired.records(HEADLINES)) == 1
    assert wired.records(sheets.WEEKEND_GAPS)[0]["weekend_start_date"] == "2026-09-18"
    assert wired.records(TRADES) == []
