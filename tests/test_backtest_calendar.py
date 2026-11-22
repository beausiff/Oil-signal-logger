from datetime import date

from scripts import backtest_calendar as bc
from src import config


def d(day):
    return date(2026, 1, day)


# 2026-01-05 is a Monday.
UP = [(d(5), 100.0), (d(6), 101.0), (d(9), 105.0)]
DOWN = [(d(12), 100.0), (d(13), 99.0), (d(16), 95.0)]


def test_one_week_up():
    t = bc.run(UP, use_stop=False)
    assert len(t) == 1
    assert t[0]["usd"] == round(5.0 - config.SPREAD_USD_PER_BBL, 4)
    assert t[0]["entry_date"] == d(5) and t[0]["exit_date"] == d(9)


def test_one_week_down():
    t = bc.run(DOWN, use_stop=False)
    assert t[0]["usd"] == round(-5.0 - config.SPREAD_USD_PER_BBL, 4)
    assert t[0]["pct"] < 0


def test_spread_is_charged():
    flat = [(d(5), 100.0), (d(9), 100.0)]
    t = bc.run(flat, use_stop=False)
    assert t[0]["usd"] == -config.SPREAD_USD_PER_BBL


def test_single_price_week_skipped():
    prices = [(d(5), 100.0)] + DOWN
    t = bc.run(prices, use_stop=False)
    assert len(t) == 1 and t[0]["entry_date"] == d(12)


def test_trailing_stop_exits_on_right_day():
    prices = [(d(5), 100.0), (d(6), 110.0), (d(7), 107.0), (d(8), 106.6), (d(9), 120.0)]
    # best 110 -> stop level 106.7; 107 is above it, 106.6 is at/below it.
    stop = bc.run(prices, use_stop=True)[0]
    assert stop["exit_date"] == d(8) and stop["reason"] == "stop" and stop["exit"] == 106.6
    nostop = bc.run(prices, use_stop=False)[0]
    assert nostop["exit_date"] == d(9) and nostop["reason"] == "friday"


def test_stop_not_hit_when_within_band():
    prices = [(d(5), 100.0), (d(6), 98.0), (d(9), 99.0)]
    assert bc.run(prices, use_stop=True)[0]["reason"] == "friday"


def test_yearly_rows_sum_to_total():
    prices = [(date(2025, 12, 22), 100.0), (date(2025, 12, 26), 103.0),
              (date(2026, 1, 5), 104.0), (date(2026, 1, 9), 101.0),
              (date(2026, 1, 12), 100.0), (date(2026, 1, 16), 106.0)]
    trades = bc.run(prices, use_stop=False)
    by_year = [bc.summarise([t for t in trades if t["year"] == y])["total_usd"] for y in (2025, 2026)]
    assert abs(sum(by_year) - bc.summarise(trades)["total_usd"]) < 1e-9
    assert len(trades) == 3


def test_max_drawdown_and_decomposition():
    assert bc.max_drawdown([5, -3, -4, 2, 10]) == 7
    prices = UP + DOWN
    dec = bc.decomposition(prices, [])
    assert abs(dec["total"] - (95.0 - 100.0)) < 1e-9
    assert abs(dec["gross"] + dec["gap"] - dec["total"]) < 1e-9


def test_luck_check_deterministic():
    a = bc.luck_check([1.0, -0.5, 0.3, 0.8, -0.2], resamples=500)
    b = bc.luck_check([1.0, -0.5, 0.3, 0.8, -0.2], resamples=500)
    assert a == b and a["lo"] <= a["hi"]
