from datetime import datetime, timedelta, timezone

import pytest

from src import outcomes
from src.sheets import SIGNALS
from tests.fakes import FakeSheets

BASE = datetime(2026, 9, 14, 12, 5, tzinfo=timezone.utc)


def row(offset_hours, price, **extra):
    record = {
        "run_utc": (BASE + timedelta(hours=offset_hours)).isoformat(),
        "brent_price": price,
        "price_1h": "", "price_4h": "", "price_24h": "", "price_72h": "",
        "move_1h_pct": "", "move_4h_pct": "", "move_24h_pct": "", "move_72h_pct": "",
    }
    record.update(extra)
    return record


def test_one_hour_and_four_hour_fill_in():
    rows = [row(0, 80.0), row(1, 81.0), row(2, 81.5), row(3, 82.0), row(4, 84.0)]
    client = FakeSheets({SIGNALS: rows})
    outcomes.backfill(client, BASE + timedelta(hours=5))

    first = client.records(SIGNALS)[0]
    assert first["price_1h"] == 81.0
    assert first["move_1h_pct"] == pytest.approx(1.25)
    assert first["price_4h"] == 84.0
    assert first["move_4h_pct"] == pytest.approx(5.0)
    assert first["price_24h"] == ""


def test_nothing_is_filled_before_the_horizon_passes():
    client = FakeSheets({SIGNALS: [row(0, 80.0), row(1, 81.0)]})
    outcomes.backfill(client, BASE + timedelta(minutes=30))
    assert client.records(SIGNALS)[0]["price_1h"] == ""


def test_a_missing_price_leaves_the_cell_blank():
    rows = [row(0, 80.0), row(1, "")]  # the +1h run had no price
    client = FakeSheets({SIGNALS: rows})
    outcomes.backfill(client, BASE + timedelta(hours=2))
    assert client.records(SIGNALS)[0]["price_1h"] == ""


def test_prices_outside_the_window_are_not_used():
    rows = [row(0, 80.0), row(1.5, 90.0)]  # 30 minutes past the target
    client = FakeSheets({SIGNALS: rows})
    outcomes.backfill(client, BASE + timedelta(hours=3))
    assert client.records(SIGNALS)[0]["price_1h"] == ""


def test_nearest_price_inside_the_window_wins():
    series = [
        (BASE + timedelta(minutes=45), 81.0),
        (BASE + timedelta(minutes=65), 82.0),
    ]
    assert outcomes.nearest_price(series, BASE + timedelta(hours=1)) == 82.0


def test_already_filled_cells_are_left_alone():
    rows = [row(0, 80.0, price_1h=79.0, move_1h_pct=-1.25), row(1, 81.0)]
    client = FakeSheets({SIGNALS: rows})
    outcomes.backfill(client, BASE + timedelta(hours=2))
    assert client.records(SIGNALS)[0]["price_1h"] == 79.0


def test_move_is_blank_when_the_signal_had_no_price():
    rows = [row(0, ""), row(1, 81.0)]
    client = FakeSheets({SIGNALS: rows})
    outcomes.backfill(client, BASE + timedelta(hours=2))
    first = client.records(SIGNALS)[0]
    assert first["price_1h"] == 81.0
    assert first["move_1h_pct"] == ""


# ------------------------------------- filling gaps from the price API --

class SpyFetcher:
    """Stands in for the price API range call and records how it was used."""

    def __init__(self, points=None):
        self.points = points or []
        self.calls = []

    def __call__(self, start, end):
        self.calls.append((start, end))
        return list(self.points)


def test_no_api_call_when_nothing_is_pending():
    client = FakeSheets({SIGNALS: [row(0, 80.0)]})
    fetcher = SpyFetcher()
    outcomes.backfill(client, BASE + timedelta(minutes=10), fetch_range=fetcher)
    assert fetcher.calls == []


def test_no_api_call_when_our_own_prices_already_cover_it():
    rows = [row(0, 80.0), row(1, 81.0)]
    client = FakeSheets({SIGNALS: rows})
    fetcher = SpyFetcher()
    # Stop short of the second row's own 1h horizon, so nothing is outstanding.
    outcomes.backfill(client, BASE + timedelta(hours=1, minutes=50), fetch_range=fetcher)

    assert fetcher.calls == []
    assert client.records(SIGNALS)[0]["price_1h"] == 81.0


def test_a_dropped_run_is_filled_from_the_api():
    """The run that would have priced the 1h target never fired."""
    rows = [row(0, 80.0), row(3, 84.0)]  # nothing logged anywhere near +1h
    client = FakeSheets({SIGNALS: rows})
    fetcher = SpyFetcher([(BASE + timedelta(hours=1), 82.5)])

    outcomes.backfill(client, BASE + timedelta(hours=4), fetch_range=fetcher)

    first = client.records(SIGNALS)[0]
    assert first["price_1h"] == 82.5
    assert first["move_1h_pct"] == pytest.approx(3.125)
    assert len(fetcher.calls) == 1, "one range call per run, not one per cell"


def test_one_call_covers_every_outstanding_horizon():
    rows = [row(0, 80.0)]
    client = FakeSheets({SIGNALS: rows})
    fetcher = SpyFetcher([
        (BASE + timedelta(hours=1), 81.0),
        (BASE + timedelta(hours=4), 82.0),
        (BASE + timedelta(hours=24), 83.0),
    ])

    outcomes.backfill(client, BASE + timedelta(hours=30), fetch_range=fetcher)

    first = client.records(SIGNALS)[0]
    assert first["price_1h"] == 81.0
    assert first["price_4h"] == 82.0
    assert first["price_24h"] == 83.0
    assert first["price_72h"] == ""  # horizon has not passed yet
    assert len(fetcher.calls) == 1


def test_api_failure_leaves_the_cell_blank_without_crashing():
    client = FakeSheets({SIGNALS: [row(0, 80.0)]})
    fetcher = SpyFetcher([])  # the API returned nothing

    assert outcomes.backfill(client, BASE + timedelta(hours=2), fetch_range=fetcher) == 0
    assert client.records(SIGNALS)[0]["price_1h"] == ""


def test_api_points_outside_the_window_are_still_rejected():
    """The API is a better source, not an excuse to accept a far away price."""
    client = FakeSheets({SIGNALS: [row(0, 80.0)]})
    fetcher = SpyFetcher([(BASE + timedelta(hours=1, minutes=45), 90.0)])

    outcomes.backfill(client, BASE + timedelta(hours=3), fetch_range=fetcher)
    assert client.records(SIGNALS)[0]["price_1h"] == ""


def test_the_range_request_is_capped_to_the_configured_days():
    """A long outage must not ask for an unbounded history."""
    old_row = row(0, 80.0)
    client = FakeSheets({SIGNALS: [old_row]})
    fetcher = SpyFetcher()
    now = BASE + timedelta(days=30)

    outcomes.backfill(client, now, fetch_range=fetcher)

    start, end = fetcher.calls[0]
    assert end == now
    assert start == now - timedelta(days=outcomes.config.OUTCOME_API_MAX_RANGE_DAYS)


def test_our_own_price_wins_when_both_sources_have_one():
    """Cheaper and identical in the normal case, so prefer what we logged."""
    rows = [row(0, 80.0), row(1, 81.0), row(2, 79.0)]
    client = FakeSheets({SIGNALS: rows})
    fetcher = SpyFetcher([(BASE + timedelta(hours=1), 999.0)])

    outcomes.backfill(client, BASE + timedelta(hours=3), fetch_range=fetcher)
    assert client.records(SIGNALS)[0]["price_1h"] == 81.0
