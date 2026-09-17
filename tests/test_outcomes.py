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
