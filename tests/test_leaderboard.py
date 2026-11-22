"""The six bot race, on synthetic rows, with real code."""

import json

import pytest

from scripts import leaderboard
from src import main, rules
from src.sheets import SIGNALS
from tests.fakes import FakeSheets
from tests.test_audit_fixes import chi_row, scored
from tests.test_main import wire_news, wire_price, fake_headline, wired  # noqa: F401


def v2(row, version="v2"):
    return dict(row, rules_version=version)


def by_key(table):
    return {b["key"]: b for b in table["bots"]}


def long_week(reversal=False):
    """Mon entry at 100; optional reversal Tue at 101; Friday close run at 103."""
    rows = [v2(scored(chi_row(2026, 9, 14, 10, 100.0), 7))]
    if reversal:
        rows.append(v2(scored(chi_row(2026, 9, 15, 10, 101.0), -5)))
    rows.append(v2(chi_row(2026, 9, 18, 15, 103.0, minute=5)))
    return rows


def test_race_rows_ignores_v1_rows():
    rows = [v2(chi_row(2026, 9, 14, 10, 100.0), "v1.1"), v2(chi_row(2026, 9, 15, 10, 101.0))]
    assert len(leaderboard.race_rows(rows)) == 1
    assert leaderboard.standings(rows[:1])["started"] is False


def test_all_six_bots_in_fixed_order():
    table = leaderboard.standings(long_week())
    assert [b["key"] for b in table["bots"]] == [
        "original", "hold_longer", "lower_bar", "lower_bar_hold", "calendar", "opposite"]


def test_opposite_shorts_where_the_original_buys_and_mirrors_it():
    bots = by_key(leaderboard.standings(long_week()))
    assert bots["original"]["trades"] == bots["opposite"]["trades"] == 1
    # Long 100 to 103 and short 100 to 103: gross mirrors, each pays the spread.
    assert bots["original"]["total_usd"] == pytest.approx(3.0 - 0.05)
    assert bots["opposite"]["total_usd"] == pytest.approx(-3.0 - 0.05)
    assert bots["original"]["total_usd"] + bots["opposite"]["total_usd"] == pytest.approx(-0.10)


def test_hold_longer_ignores_a_reversal():
    bots = by_key(leaderboard.standings(long_week(reversal=True)))
    assert bots["original"]["total_usd"] == pytest.approx(1.0 - 0.05)    # out at 101
    assert bots["hold_longer"]["total_usd"] == pytest.approx(3.0 - 0.05)  # out at the Friday close


def test_an_open_position_reports_unrealised_pnl():
    rows = [v2(scored(chi_row(2026, 9, 14, 10, 100.0), 7)), v2(chi_row(2026, 9, 14, 12, 102.0))]
    bot = by_key(leaderboard.standings(rows))["original"]
    assert bot["trades"] == 0
    assert bot["open"]["side"] == "long"
    assert bot["open"]["entry_price"] == 100.0
    assert bot["open"]["unrealised_usd"] == pytest.approx(rules.pnl("long", 100.0, 102.0)[0])
    assert bot["luck_p"] is None
    assert leaderboard.standings(rows)["benchmark"]["usd"] == pytest.approx(2.0)


def test_luck_p_is_none_below_ten_trades():
    table = leaderboard.standings(long_week())
    assert all(b["luck_p"] is None for b in table["bots"])
    assert by_key(table)["original"]["luck_note"] == "need 10 trades"
    assert by_key(table)["calendar"]["luck_note"].startswith("n/a")


def test_main_with_no_v2_rows_exits_zero(monkeypatch, capsys):
    client = FakeSheets({SIGNALS: [v2(chi_row(2026, 9, 14, 10, 100.0), "v1.1")]})
    monkeypatch.setattr(leaderboard.sheets, "open_client", lambda dry_run=False: client)
    assert leaderboard.main() == 0
    assert "race has not started: no v2 rows yet" in capsys.readouterr().out


def test_main_writes_the_json_and_the_tab(monkeypatch, tmp_path, capsys):
    client = FakeSheets({SIGNALS: long_week()})
    monkeypatch.setattr(leaderboard.sheets, "open_client", lambda dry_run=False: client)
    monkeypatch.chdir(tmp_path)
    assert leaderboard.main() == 0
    data = json.loads((tmp_path / "backup" / "leaderboard.json").read_text())
    assert [b["key"] for b in data["bots"]][0] == "original"
    assert "leaderboard" in client.tables


def test_write_table_creates_a_missing_tab_in_the_fake():
    client = FakeSheets()
    assert "leaderboard" not in client.tables
    client.write_table("leaderboard", [["a", 1]])
    assert client.tables["leaderboard"] == [["a", 1]]


def test_the_tab_layout():
    client = FakeSheets()
    leaderboard.write_tab(client, leaderboard.standings(long_week(reversal=True)),
                          updated_utc="2026-11-24T00:00:00+00:00")
    rows = client.tables["leaderboard"]
    assert rows[0][0] == "rank" and rows[1][0] == 1
    assert rows[0][2] == "$100 is now"
    balances = [r[2] for r in rows[1:7]]
    assert balances == sorted(balances, reverse=True)
    assert rows[7] == []
    assert rows[8][0] == "benchmark"
    assert rows[9] == ["updated_utc", "2026-11-24T00:00:00+00:00"]


def test_a_leaderboard_failure_does_not_fail_the_run(wired, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("leaderboard exploded")

    monkeypatch.setattr(leaderboard, "standings", boom)
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    from tests.test_main import wire_score
    wire_score(monkeypatch, 2, "low")
    assert main.run() == 0
    assert len(wired.records(SIGNALS)) == 1


def test_a_normal_run_writes_the_leaderboard_tab(wired, monkeypatch):  # noqa: F811
    wire_news(monkeypatch, [fake_headline()])
    wire_price(monkeypatch, 80.0)
    from tests.test_main import wire_score
    wire_score(monkeypatch, 2, "low")
    main.run()
    assert "leaderboard" in wired.tables


# ------------------------------------------------------- the $100 balance --

def test_balance_compounds_the_trades_own_pnl_pct():
    trades = [{"pnl_pct": 10.0}, {"pnl_pct": -10.0}]
    assert leaderboard.compound(trades) == pytest.approx(99.0, abs=0.01)
    table = leaderboard.standings(long_week(reversal=True))
    bot = by_key(table)["original"]
    real_trades, _ = leaderboard.run_bot(leaderboard.BOTS[0], leaderboard.race_rows(long_week(reversal=True)))
    expected = 100.0
    for t in real_trades:
        expected *= 1 + t["pnl_pct"] / 100.0
    assert bot["balance"] == pytest.approx(expected, abs=0.01)
    assert bot["balance_now"] == pytest.approx(expected, abs=0.01)


def open_long_rows():
    """Bought at 100 on Monday, no exit yet, last price 105."""
    return [v2(scored(chi_row(2026, 9, 14, 10, 100.0), 7)), v2(chi_row(2026, 9, 14, 12, 105.0))]


def test_an_open_position_marks_to_market_in_balance_now_but_not_balance():
    bot = by_key(leaderboard.standings(open_long_rows()))["original"]
    assert bot["open"] is not None
    assert bot["balance"] == 100.0
    pnl_pct = rules.pnl("long", 100.0, 105.0)[1]
    assert bot["balance_now"] == pytest.approx(100.0 * (1 + pnl_pct / 100.0), abs=0.01)
    assert bot["balance_now"] > 104


def test_benchmark_balance_is_100_times_last_over_first():
    rows = [v2(chi_row(2026, 9, 14, 10, 100.0)), v2(chi_row(2026, 9, 15, 10, 110.0)),
            v2(chi_row(2026, 9, 16, 10, 95.0))]
    assert leaderboard.standings(rows)["benchmark"]["balance_now"] == pytest.approx(95.0)


def test_ranked_orders_by_balance_now_with_ties_in_bot_number_order():
    flat = leaderboard.standings([v2(chi_row(2026, 9, 14, 10, 100.0)), v2(chi_row(2026, 9, 14, 11, 100.0))])
    even = {"bots": [dict(b, balance_now=100.0) for b in flat["bots"]]}
    assert [b["key"] for b in leaderboard.ranked(even)] == [b["key"] for b in leaderboard.BOTS]
    table = {"bots": [dict(b, balance_now=v) for b, v in zip(flat["bots"], (100, 90, 120, 100, 100, 100))]}
    order = [b["key"] for b in leaderboard.ranked(table)]
    assert order == ["lower_bar", "original", "lower_bar_hold", "calendar", "opposite", "hold_longer"]
