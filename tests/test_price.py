"""The range response envelope is not pinned down in the public docs, so the
parser accepts the plausible shapes rather than guessing one."""

from datetime import datetime, timezone

import pytest

from src import price


def ts(hour):
    return datetime(2026, 9, 17, hour, 0, tzinfo=timezone.utc)


ROWS = [
    {"price": 103.5, "created_at": "2026-09-17T10:00:00Z"},
    {"price": 104.0, "created_at": "2026-09-17T11:00:00Z"},
]


@pytest.mark.parametrize(
    "payload",
    [
        ROWS,
        {"data": ROWS},
        {"data": {"prices": ROWS}},
        {"data": {"results": ROWS}},
        {"data": {"items": ROWS}},
    ],
)
def test_every_plausible_envelope_parses(payload):
    points = price._price_points(payload)
    assert points == [(ts(10), 103.5), (ts(11), 104.0)]


def test_points_come_back_oldest_first():
    payload = {"data": list(reversed(ROWS))}
    points = price._price_points(payload)
    assert [p[0] for p in points] == [ts(10), ts(11)]


@pytest.mark.parametrize(
    "key", ["created_at", "source_timestamp", "as_of", "updated_at", "timestamp"]
)
def test_each_timestamp_key_is_understood(key):
    points = price._price_points([{"price": 99.0, key: "2026-09-17T10:00:00Z"}])
    assert points == [(ts(10), 99.0)]


def test_unix_seconds_are_understood():
    epoch = int(ts(10).timestamp())
    points = price._price_points([{"price": 99.0, "timestamp": str(epoch)}])
    assert points == [(ts(10), 99.0)]


@pytest.mark.parametrize(
    "payload",
    [None, {}, [], {"data": None}, {"data": {}}, "not json", {"data": "nope"}],
)
def test_junk_yields_no_points_instead_of_raising(payload):
    assert price._price_points(payload) == []


def test_rows_missing_a_price_or_time_are_skipped():
    payload = [
        {"price": None, "created_at": "2026-09-17T10:00:00Z"},
        {"price": "abc", "created_at": "2026-09-17T10:00:00Z"},
        {"price": 100.0},
        {"price": 101.0, "created_at": "2026-09-17T11:00:00Z"},
    ]
    assert price._price_points(payload) == [(ts(11), 101.0)]


def test_no_key_means_no_range_call(monkeypatch):
    monkeypatch.delenv("OILPRICE_API_KEY", raising=False)
    assert price.fetch_brent_range(ts(10), ts(11)) == []
