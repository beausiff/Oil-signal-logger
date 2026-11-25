"""Bots 7 to 14 and the two cohorts, on synthetic rows, with real code.

Also the golden test: bots 1 to 6 must produce exactly what they produced on
2026-11-25 before any of this existed.
"""

import csv
import json
import os

import pytest

from scripts import leaderboard, replay
from src import config, rules
from src.sheets import SIGNALS
from tests.fakes import FakeSheets
from tests.test_audit_fixes import chi_row

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "fixtures", "golden_bots_1_to_6.json")
GOLDEN_INPUT = os.path.join(HERE, "fixtures", "golden_signals_input.csv")

ALL = {b["key"]: b for b in leaderboard.ALL_BOTS}
REF = leaderboard.REFERENCE_BOT


def row(day, hour, price, score=None, minute=0, version="v2", ctx=True, vol=None, ma=None, flags="", **extra):
    """A Chicago time row in September 2026 (14 is a Monday). ctx=True adds
    context_version c1 and the three context columns."""
    r = chi_row(2026, 9, day, hour, price, minute=minute)
    r["rules_version"] = version
    if score is not None:
        r["score"], r["confidence"] = str(score), "high"
    if ctx:
        r["context_version"] = "c1"
        r["vol20_pct"] = "" if vol is None else str(vol)
        r["ma200"] = "" if ma is None else str(ma)
        r["event_flags"] = flags
    r.update(extra)
    return r


def run(key, rows):
    return leaderboard.run_bot(ALL[key], rows)


def run_full(key, rows):
    return leaderboard.run_bot_full(ALL[key], rows)


# ----------------------------------------------------------- golden test --

def load_golden():
    with open(GOLDEN, encoding="utf-8") as handle:
        golden = json.load(handle)
    with open(GOLDEN_INPUT, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return golden, rows


def jsonable(value):
    return json.loads(json.dumps(value))


def position_dict(position):
    import dataclasses
    return dataclasses.asdict(position)


def cohort1_view(table):
    """The table as it was before cohorts: no cohort field, no cohort2 section."""
    out = {k: v for k, v in table.items() if k != "cohort2"}
    out["bots"] = [{k: v for k, v in b.items() if k != "cohort"} for b in table["bots"]]
    return out


def test_bots_1_to_6_golden_trades_and_positions_are_exactly_unchanged():
    golden, rows = load_golden()
    race = leaderboard.race_rows(rows)
    everything = sorted(rows, key=lambda r: str(r.get("run_utc", "")))
    assert sorted(b["key"] for b in leaderboard.BOTS) == sorted(golden["bots"])
    for bot in leaderboard.BOTS:
        for name, subset in (("race_rows", race), ("all_rows", everything)):
            trades, position = leaderboard.run_bot(bot, subset)
            expected = golden["bots"][bot["key"]][name]
            assert jsonable(trades) == expected["trades"], (bot["key"], name)
            assert jsonable(position_dict(position)) == expected["position"], (bot["key"], name)
            assert len(subset) == expected["input_rows"]


def test_the_golden_input_actually_exercises_the_bots():
    golden, _ = load_golden()
    counts = {k: len(v["all_rows"]["trades"]) for k, v in golden["bots"].items()}
    assert all(n >= 4 for n in counts.values()), counts


def test_replay_output_is_exactly_unchanged():
    golden, rows = load_golden()
    everything = sorted(rows, key=lambda r: str(r.get("run_utc", "")))
    trades, position, skipped = replay.replay(everything)
    expected = golden["replay_all_rows"]
    assert jsonable(trades) == expected["trades"]
    assert jsonable(position_dict(position)) == expected["position"]
    assert skipped == expected["skipped_no_price"]
    assert replay.replay_variant(everything) == replay.replay(everything)


def test_standings_for_bots_1_to_6_are_identical_including_luck_p():
    """Luck p is deterministic: every circular shift is run, nothing is drawn."""
    golden, rows = load_golden()
    relabelled = [dict(r, rules_version="v2") for r in rows]
    assert cohort1_view(leaderboard.standings(rows, with_luck=False)) == golden["standings_actual"]
    assert cohort1_view(leaderboard.standings(relabelled, with_luck=False)) == golden["standings_all_v2_no_luck"]
    with_luck = leaderboard.standings(relabelled, with_luck=True)
    assert cohort1_view(with_luck) == golden["standings_all_v2_luck"]
    p = {b["key"]: b["luck_p"] for b in with_luck["bots"]}
    assert p["lower_bar"] is not None and p["lower_bar_hold"] is not None   # the luck path really ran


def test_the_six_registered_bots_are_untouched_and_the_new_ones_follow():
    assert [b["key"] for b in leaderboard.BOTS] == [
        "original", "hold_longer", "lower_bar", "lower_bar_hold", "calendar", "opposite"]
    assert [b["key"] for b in leaderboard.NEW_BOTS] == [
        "wide_stop", "vol_stop", "short_hold", "fade_24h", "trend_filter",
        "event_standaside", "vol_size", "safeguards", "trend_bar", "trend_only"]
    assert [b["key"] for b in leaderboard.ALL_BOTS][:6] == [b["key"] for b in leaderboard.BOTS]
    assert len(leaderboard.ALL_BOTS) == 16
    assert leaderboard.BOTS[5] == {"key": "opposite", "label": "Bot 6: Opposite", "variant": {}, "negate": True}


def test_replay_variant_defaults_reproduce_the_locked_replay():
    rows = [row(14, 10, 100.0, 7, ctx=False), row(14, 12, 103.0, ctx=False),
            row(15, 10, 101.0, -5, ctx=False), row(16, 10, 99.0, -7, ctx=False),
            row(18, 15, 98.0, minute=5, ctx=False)]
    assert replay.replay_variant(rows) == replay.replay(rows)
    assert replay.replay_variant(rows, stop_pct=None, vol_stop_mult=None, max_hold_hours=None,
                                 trend_filter=False, event_standaside=False, vol_size=False) == replay.replay(rows)
    trades = replay.replay(rows)[0]
    assert all("size" not in t and "stop_pct" not in t for t in trades)


def test_config_is_restored_after_a_stop_override():
    rows = [row(14, 10, 100.0, 7), row(14, 12, 90.0)]
    before = config.TRAILING_STOP_PCT
    replay.replay_variant(rows, stop_pct=8.0)
    replay.replay_variant(rows, vol_stop_mult=3.0, entry=4)
    assert config.TRAILING_STOP_PCT == before
    assert (config.ENTRY_LONG_SCORE, config.ENTRY_SHORT_SCORE) == (6, -6)


# ------------------------------------------------------------ Bot 7 ------

def drop_rows():
    return [row(14, 10, 100.0, 7), row(14, 11, 100.0), row(14, 12, 94.9), row(14, 13, 91.0)]


def test_wide_stop_survives_a_5_percent_drop_that_stops_bot_1():
    original, _ = run("original", drop_rows())
    assert [(t["reason"], t["exit_price"]) for t in original] == [("stop", 94.9)]
    wide, position = run("wide_stop", drop_rows()[:3])
    assert wide == [] and position.is_open and position.entry_price == 100.0
    wide, position = run("wide_stop", drop_rows())
    assert [(t["reason"], t["exit_price"], t["stop_pct"]) for t in wide] == [("stop", 91.0, 8.0)]
    assert not position.is_open


# ------------------------------------------------------------ Bot 8 ------

def vol_rows(vol, later_vol=None, to=100.0, extra_to=None):
    rows = [row(14, 10, 100.0, 7, vol=vol), row(14, 11, 100.0, vol=later_vol), row(14, 12, to, vol=later_vol)]
    if extra_to is not None:
        rows.append(row(14, 13, extra_to, vol=later_vol))
    return rows


def test_vol_stop_clamps_at_3_and_15_and_scales_between():
    trades, _ = run("vol_stop", vol_rows(0.5, to=96.9))      # 1.5 -> 3
    assert [(t["reason"], t["stop_pct"]) for t in trades] == [("stop", 3.0)]
    trades, position = run("vol_stop", vol_rows(2.0, to=95.0))  # 6
    assert trades == [] and position.is_open
    trades, _ = run("vol_stop", vol_rows(2.0, to=95.0, extra_to=93.9))
    assert [(t["reason"], t["stop_pct"]) for t in trades] == [("stop", 6.0)]
    trades, position = run("vol_stop", vol_rows(10.0, to=86.0))  # 30 -> 15, 14% down is inside it
    assert trades == [] and position.is_open
    trades, _ = run("vol_stop", vol_rows(10.0, to=86.0, extra_to=84.9))
    assert [(t["reason"], t["stop_pct"]) for t in trades] == [("stop", 15.0)]


def test_vol_stop_width_is_fixed_at_entry():
    # Vol jumps to 10 after entry, which would mean 15%. The trade keeps its 6%.
    trades, _ = run("vol_stop", vol_rows(2.0, later_vol=10.0, to=95.0, extra_to=93.9))
    assert [(t["reason"], t["stop_pct"]) for t in trades] == [("stop", 6.0)]


# ------------------------------------------------------------ Bots 9, 10 --

def hold_rows(second_day, second_hour, third_day, third_hour, score=7):
    return [row(14, 10, 100.0, score, ctx=False), row(second_day, second_hour, 101.0, ctx=False),
            row(third_day, third_hour, 102.0, ctx=False)]


def test_short_hold_exits_at_the_first_row_at_or_after_24_hours():
    rows = hold_rows(15, 9, 15, 12)           # 23 hours, then 26
    trades, position = run("short_hold", rows)
    assert not position.is_open
    assert [(t["reason"], t["exit_price"], t["hours"]) for t in trades] == [("time", 102.0, 26.0)]
    assert run("original", rows)[1].is_open    # Bot 1 is still holding
    exactly = run("short_hold", hold_rows(15, 9, 15, 10))[0]
    assert [(t["reason"], t["hours"]) for t in exactly] == [("time", 24.0)]


def test_time_exit_waits_for_an_open_market_row():
    rows = [row(16, 10, 100.0, 7, ctx=False), row(17, 16, 101.0, minute=30, ctx=False),
            row(17, 17, 102.0, minute=30, ctx=False)]
    trades, _ = run("short_hold", rows)
    assert [(t["exit_utc"] == rows[2]["run_utc"], t["reason"]) for t in trades] == [(True, "time")]
    assert run("short_hold", rows[:2])[1].is_open   # 30.5 hours in, but the break hour is closed


def test_the_friday_close_beats_the_time_exit():
    rows = [row(17, 10, 100.0, 7, ctx=False), row(18, 15, 101.0, minute=5, ctx=False)]
    trades, _ = run("short_hold", rows)
    assert [t["reason"] for t in trades] == ["friday"]


def test_fade_24h_is_the_opposite_bot_with_the_time_exit():
    rows = [row(14, 10, 100.0, 7, ctx=False), row(15, 10, 99.0, ctx=False)]
    trades, position = run("fade_24h", rows)
    assert not position.is_open
    assert [(t["side"], t["reason"], t["hours"]) for t in trades] == [("short", "time", 24.0)]
    assert trades[0]["pnl_usd"] == pytest.approx(1.0 - config.SPREAD_USD_PER_BBL)
    assert run("opposite", rows)[1].is_open


# ------------------------------------------------------------ Bot 11 -----

def test_trend_filter_blocks_a_long_below_ma200_and_a_short_above_it():
    below = [row(14, 10, 100.0, 7, ma=105.0), row(14, 11, 101.0, ma=105.0)]
    trades, position = run("trend_filter", below)
    assert trades == [] and not position.is_open
    assert run("original", below)[1].is_open
    above = [row(14, 10, 100.0, 7, ma=95.0)]
    assert run("trend_filter", above)[1].side == "long"
    short_blocked = [row(14, 10, 100.0, -7, ma=95.0)]
    assert not run("trend_filter", short_blocked)[1].is_open
    short_ok = [row(14, 10, 100.0, -7, ma=105.0)]
    assert run("trend_filter", short_ok)[1].side == "short"
    equal = [row(14, 10, 100.0, 7, ma=100.0)]
    assert not run("trend_filter", equal)[1].is_open        # strictly above


def test_trend_filter_leaves_exits_alone():
    rows = [row(14, 10, 100.0, 7, ma=95.0), row(14, 12, 101.0, -5, ma=120.0)]
    trades, _ = run("trend_filter", rows)
    assert [t["reason"] for t in trades] == ["reversal"]


# ------------------------------------------------------------ Bot 12 -----

def test_event_standaside_blocks_entries_on_flagged_rows_but_not_exits():
    rows = [row(14, 10, 100.0, 7), row(14, 11, 101.0, -5, flags="opec_day;other"),
            row(14, 12, 101.0, 7, flags="eia_window"), row(14, 13, 102.0, 7)]
    trades, position, info = run_full("event_standaside", rows)
    assert [t["reason"] for t in trades] == ["reversal"]      # the exit on a flagged row still happened
    assert position.is_open and position.entry_price == 102.0  # the entry waited for an unflagged row
    assert info["blocked_entries"] == {"event": 1}
    original_trades, original_position = run("original", rows)
    assert original_position.entry_price == 101.0              # Bot 1 took the flagged entry


def test_event_flags_blank_on_a_context_row_means_no_event():
    rows = [row(14, 10, 100.0, 7, flags="")]
    _, position, info = run_full("event_standaside", rows)
    assert position.is_open and info["missing_input_decisions"] == 0


# ------------------------------------------------------------ Bot 13 -----

def sized_rows(vol):
    return [row(14, 10, 100.0, 7, vol=vol), row(14, 12, 104.0, -5, vol=vol)]


def test_vol_size_scales_pnl_and_clamps_the_size():
    for vol, size in ((4.0, 0.5), (1.0, 2.0), (0.5, 2.0), (100.0, 0.25), (2.0, 1.0)):
        trades, _ = run("vol_size", sized_rows(vol))
        assert trades[0]["size"] == size, vol
        assert trades[0]["pnl_pct"] == run("original", sized_rows(vol))[0][0]["pnl_pct"]   # stored per unit


def test_vol_size_totals_and_balance_use_pnl_times_size():
    rows = [dict(r, rules_version="v2") for r in sized_rows(4.0)]
    section = leaderboard.standings(rows, with_luck=False)["cohort2"]
    bot = {b["key"]: b for b in section["bots"]}["vol_size"]
    ref = section["reference"]
    assert ref["total_usd"] == pytest.approx(4.0 - 0.05)
    assert bot["total_usd"] == pytest.approx((4.0 - 0.05) * 0.5)
    assert bot["total_pct"] == pytest.approx((3.95) * 0.5)
    assert bot["balance"] == pytest.approx(100.0 * (1 + 3.95 * 0.5 / 100.0), abs=1e-3)
    assert leaderboard.compound([{"pnl_pct": 10.0, "size": 0.5}]) == pytest.approx(105.0)
    assert leaderboard.compound([{"pnl_pct": 10.0}]) == pytest.approx(110.0)


def test_an_open_sized_position_marks_to_market_at_its_size():
    rows = [row(14, 10, 100.0, 7, vol=4.0), row(14, 12, 104.0, vol=4.0)]
    section = leaderboard.standings(rows, with_luck=False)["cohort2"]
    bot = {b["key"]: b for b in section["bots"]}["vol_size"]
    assert bot["open"]["size"] == 0.5
    pct = rules.pnl("long", 100.0, 104.0)[1]
    assert bot["balance_now"] == pytest.approx(100.0 * (1 + pct * 0.5 / 100.0), abs=1e-3)
    assert bot["open"]["unrealised_usd"] == pytest.approx(rules.pnl("long", 100.0, 104.0)[0] * 0.5)


def test_without_a_size_the_numbers_are_identical_to_bot_1():
    rows = [row(14, 10, 100.0, 7), row(14, 12, 104.0, -5)]       # no vol at all
    sized, _, info = run_full("vol_size", rows)
    plain, _ = run("original", rows)
    assert sized[0]["size"] == 1.0
    assert leaderboard.sized_usd(sized[0]) == plain[0]["pnl_usd"]
    assert leaderboard.compound(sized) == leaderboard.compound(plain)
    assert info["missing_input_decisions"] == 1


# ------------------------------------------------------------ Bot 14 -----

def test_safeguards_combines_the_four_features():
    # A: below ma200, blocked by the trend filter. B: flagged. C: all clear, vol 4 -> size 0.5, stop 12%.
    rows = [row(14, 10, 100.0, 7, vol=4.0, ma=105.0),
            row(14, 11, 100.0, 7, vol=4.0, ma=95.0, flags="opec_day"),
            row(14, 12, 100.0, 7, vol=4.0, ma=95.0),
            row(14, 13, 89.0, vol=4.0, ma=95.0),      # 11% down: inside a 12% stop, outside Bot 1's 3%
            row(14, 14, 87.9, vol=4.0, ma=95.0)]      # 12.1% down
    trades, position, info = run_full("safeguards", rows)
    assert info["blocked_entries"] == {"trend": 1, "event": 1}
    assert [(t["entry_utc"] == rows[2]["run_utc"], t["stop_pct"], t["size"], t["reason"]) for t in trades] == \
        [(True, 12.0, 0.5, "stop")]
    assert not position.is_open


# ------------------------------------------------- the missing input rule -

def test_the_most_recent_non_blank_value_is_carried_forward():
    rows = [row(14, 9, 100.0, vol=4.0, ma=95.0), row(14, 10, 100.0, 7), row(14, 12, 104.0, -5)]
    trades, _, info = run_full("safeguards", rows)
    assert trades[0]["size"] == 0.5 and trades[0]["stop_pct"] == 12.0
    assert info["missing_input_decisions"] == 0
    trades, _, info = run_full("vol_size", rows)
    assert trades[0]["size"] == 0.5


def test_a_feature_with_no_value_ever_is_skipped_and_counted():
    rows = [row(14, 10, 100.0, 7, ma=95.0), row(14, 12, 104.0, -5, ma=95.0)]   # no vol, flags known
    trades, _, info = run_full("safeguards", rows)
    assert trades[0]["size"] == 1.0 and trades[0]["stop_pct"] == config.TRAILING_STOP_PCT
    assert info["missing_input_decisions"] == 1
    assert info["missing_by_feature"] == {"vol20_pct": 1}

    rows = [row(14, 10, 100.0, 7, vol=2.0, ctx=True)]                           # no ma200
    plain, _ = run("original", rows)
    position = run("trend_filter", rows)[1]
    assert position.is_open                                                      # behaves like Bot 1 for it
    assert run_full("trend_filter", rows)[2]["missing_by_feature"] == {"ma200": 1}

    rows = [row(14, 10, 100.0, 7, ctx=False)]                                    # an old row: nothing at all
    _, position, info = run_full("event_standaside", rows)
    assert position.is_open and info["missing_by_feature"] == {"event_flags": 1}


def test_a_blocked_or_unneeded_decision_is_not_counted_as_missing():
    rows = [row(14, 10, 100.0, 7, vol=2.0, ma=95.0)]
    assert run_full("vol_stop", rows)[2]["missing_input_decisions"] == 0
    assert run_full("original", rows)[2]["missing_input_decisions"] == 0


def test_missing_input_decisions_appear_in_the_bot_output():
    rows = [row(14, 10, 100.0, 7)]
    section = leaderboard.standings(rows, with_luck=False)["cohort2"]
    by_key = {b["key"]: b for b in section["bots"]}
    assert by_key["vol_size"]["missing_input_decisions"] == 1
    assert by_key["safeguards"]["missing_by_feature"] == {"ma200": 1, "vol20_pct": 1}
    assert by_key["wide_stop"]["missing_input_decisions"] == 0
    assert section["reference"]["missing_input_decisions"] == 0


# ----------------------------------------------------------- cohorts -----

def cohort_rows():
    """Two v2 rows with no context, then the first c1 row. Bot 1 buys on the
    first row, the cohort 2 bots can only start at the third."""
    return [row(14, 10, 100.0, 7, ctx=False), row(14, 11, 101.0, ctx=False),
            row(14, 12, 102.0, 7, vol=2.0, ma=90.0), row(14, 13, 103.0, vol=2.0, ma=90.0)]


def test_cohort_2_starts_at_the_first_c1_row_and_each_bot_starts_flat():
    rows = cohort_rows() + [row(1, 9, 90.0, version="v1.1", ctx=False)]
    assert leaderboard.cohort2_rows(rows) == leaderboard.race_rows(rows)[2:]
    table = leaderboard.standings(rows, with_luck=False)
    section = table["cohort2"]
    assert section["started"] and section["state"] == "running"
    assert section["rows"] == 2 and section["start_utc"] == rows[2]["run_utc"]
    assert table["race_rows"] == 4
    original = {b["key"]: b for b in table["bots"]}["original"]
    assert original["open"]["entry_price"] == 100.0           # cohort 1 is untouched
    wide = {b["key"]: b for b in section["bots"]}["wide_stop"]
    assert wide["open"]["entry_price"] == 102.0               # cohort 2 entered at its own first row
    assert wide["cohort"] == 2 and original["cohort"] == 1


def test_the_reference_is_bot_1_over_the_cohort_2_window():
    rows = cohort_rows()
    section = leaderboard.standings(rows, with_luck=False)["cohort2"]
    ref = section["reference"]
    assert ref["key"] == "original_same_window" and ref["label"] == "Bot 1 over the same window"
    assert ref["reference"] is True and ref["cohort"] == 2
    window = leaderboard.cohort2_rows(rows)
    trades, position = leaderboard.run_bot(leaderboard.BOTS[0], window)
    assert ref["trades"] == len(trades)
    assert ref["open"]["entry_price"] == position.entry_price == 102.0
    assert ref["balance_now"] == leaderboard._bot_entry(leaderboard.BOTS[0], window, 103.0, False)["balance_now"]
    assert "reference" not in {b["key"]: b for b in section["bots"]}["wide_stop"]
    assert ref["key"] not in [b["key"] for b in section["bots"]]


def test_the_reference_equals_a_new_bot_that_changes_nothing_on_these_rows():
    rows = [row(14, 10, 100.0, 7, vol=2.0, ma=90.0), row(14, 12, 104.0, -5, vol=2.0, ma=90.0),
            row(15, 10, 100.0, 7, vol=2.0, ma=90.0), row(15, 12, 103.0, -5, vol=2.0, ma=90.0)]
    section = leaderboard.standings(rows, with_luck=False)["cohort2"]
    ref = section["reference"]
    by_key = {b["key"]: b for b in section["bots"]}
    for key in ("wide_stop", "trend_filter", "event_standaside"):
        for field in ("trades", "total_usd", "total_pct", "balance_now", "winners"):
            assert by_key[key][field] == ref[field], (key, field)


def test_no_c1_rows_gives_a_clean_not_started_state():
    rows = [row(14, 10, 100.0, 7, ctx=False), row(14, 12, 104.0, -5, ctx=False)]
    assert leaderboard.cohort2_rows(rows) == []
    table = leaderboard.standings(rows)
    section = table["cohort2"]
    assert table["started"] and not section["started"] and section["state"] == "not started"
    assert [b["key"] for b in section["bots"]] == [b["key"] for b in leaderboard.NEW_BOTS]
    assert all(b["trades"] == 0 and b["state"] == "not started" and b["luck_note"] == "not started"
               and b["balance_now"] == 100.0 and b["open"] is None for b in section["bots"])
    assert section["reference"]["state"] == "not started" and section["reference"]["trades"] == 0
    assert {b["key"]: b for b in table["bots"]}["original"]["trades"] == 1   # cohort 1 still races
    client = FakeSheets()
    leaderboard.write_tab(client, table, updated_utc="x")
    flat = [cell for r in client.tables["leaderboard"] for cell in r]
    assert "not started" in flat
    assert leaderboard.bot_trades_report(rows)["cohort2_state"] == "not started"
    json.dumps(table)


def test_no_v2_rows_at_all_still_has_a_not_started_cohort_2():
    table = leaderboard.standings([row(14, 10, 100.0, 7, version="v1.1")])
    assert table["started"] is False and table["bots"] == []
    assert table["cohort2"]["started"] is False


def test_the_cohort_1_table_shape_is_additive_only():
    table = leaderboard.standings(cohort_rows(), with_luck=False)
    assert set(table) == {"started", "bots", "benchmark", "race_rows", "race_start", "generated_utc", "cohort2"}
    keys = {"key", "label", "trades", "winners", "total_usd", "total_pct", "balance", "balance_now",
            "worst_usd", "open", "luck_p", "luck_note", "cohort"}
    assert all(set(b) == keys for b in table["bots"])
    assert [b["key"] for b in table["bots"]] == [b["key"] for b in leaderboard.BOTS]


# ------------------------------------------------------ luck p and shuffle -

def long_run(n_days=20):
    """Deterministic pseudo random rows over many weekdays, with context."""
    rows, seed, price = [], 12345, 100.0
    day = 0
    for d in range(n_days + 8):
        date = 14 + d
        # Sept has 30 days, so roll into October through chi_row's own datetime.
        month, dom = (9, date) if date <= 30 else (10, date - 30)
        weekday = (date - 14) % 7
        if weekday >= 5:
            continue
        for hour in (9, 11, 13):
            seed = (seed * 1103515245 + 12345) % (2 ** 31)
            price = max(50.0, price + (seed % 400 - 200) / 100.0)
            score = (seed // 7) % 21 - 10
            r = chi_row(2026, month, dom, hour, round(price, 2))
            r.update(rules_version="v2", score=str(score), confidence="high", context_version="c1",
                     vol20_pct=str(1.0 + (seed % 40) / 10.0), ma200=str(round(price - 3 + seed % 7, 2)),
                     event_flags="eia_window" if seed % 11 == 0 else "")
            rows.append(r)
        day += 1
    return rows


def test_the_shuffle_moves_only_score_side_fields_and_keeps_price_side_columns():
    rows = long_run()
    seen = []

    def spy(scratch):
        seen.append([dict(r) for r in scratch])
        return ([],)

    replay.shuffled_strategy(rows, runner=spy)
    assert len(seen) == len(rows) - 2 * 10 + 1
    for scratch in seen:
        for before, after in zip(rows, scratch):
            for column in ("brent_price", "ma200", "vol20_pct", "event_flags", "run_utc", "run_chicago",
                           "context_version"):
                assert before[column] == after[column]
    assert any(s[0]["score"] != rows[0]["score"] for s in seen)
    assert rows[0]["score"] == long_run()[0]["score"]          # the real rows are never mutated


def test_luck_p_runs_for_every_new_bot_and_is_deterministic():
    rows = long_run()
    assert len(rows) >= leaderboard.MIN_ROWS_FOR_LUCK
    for bot in leaderboard.NEW_BOTS + [REF]:
        trades, _ = leaderboard.run_bot(bot, rows)
        total = sum(leaderboard.sized_usd(t) for t in trades)
        a = leaderboard.luck_p(bot, rows, total)
        b = leaderboard.luck_p(bot, rows, total)
        assert a is not None and 0.0 < a <= 1.0 and a == b, bot["key"]


def test_the_negate_variant_is_negated_after_the_shift_and_the_size_variant_is_sized():
    rows = long_run()
    bot = ALL["fade_24h"]
    # The runner a luck run uses must see the same thing the real run does: a
    # scratch copy with shifted scores, negated inside run_bot.
    scratch = [dict(r, score=str(-int(r["score"]))) for r in rows]
    assert leaderboard.run_bot(bot, rows)[0] == replay.replay_variant(
        leaderboard.negated(rows), max_hold_hours=24)[0]
    assert leaderboard.negated(scratch)[0]["score"] == rows[0]["score"]
    sized_bot = ALL["vol_size"]
    trades, _ = leaderboard.run_bot(sized_bot, rows)
    assert any(t["size"] != 1.0 for t in trades)
    scaled = leaderboard._sized_trades(trades)
    assert [t["pnl_usd"] for t in scaled] == [leaderboard.sized_usd(t) for t in trades]
    assert [t["pnl_usd"] for t in trades] != [t["pnl_usd"] for t in scaled]


def test_the_standings_luck_path_runs_for_cohort_2():
    section = leaderboard.standings(long_run(), with_luck=True)["cohort2"]
    for bot in section["bots"] + [section["reference"]]:
        if bot["key"] == "trend_only":
            assert bot["luck_p"] is None and bot["luck_note"].startswith("n/a")
        elif bot["trades"] >= leaderboard.MIN_TRADES_FOR_LUCK:
            assert bot["luck_p"] is not None, bot["key"]
        else:
            assert bot["luck_note"] == "need %d trades" % leaderboard.MIN_TRADES_FOR_LUCK


# --------------------------------------------------- the trade log --------

def logged_rows():
    return [row(14, 10, 100.0, 7, vol=2.0, ma=95.0, flags="opec_day"),
            row(14, 11, 101.0, vol=2.0, ma=95.0),
            row(14, 12, 94.9, vol=2.0, ma=95.0),
            row(14, 13, 91.0, vol=2.0, ma=95.0),
            row(15, 10, 92.0, 7, vol=2.0, ma=95.0)]       # a fresh entry, left open


def records_for(rows, bot):
    return [t for t in leaderboard.bot_trades_report(rows)["trades"] if t["bot"] == bot]


def test_every_closed_trade_and_open_position_is_logged_with_its_context():
    report = leaderboard.bot_trades_report(logged_rows())
    assert report["columns"] == leaderboard.BOT_TRADE_COLUMNS
    assert report["cohort2_state"] == "running"
    bots = {t["bot"] for t in report["trades"]}
    assert bots >= {"original", "wide_stop", "vol_stop", "original_same_window", "calendar"}

    wide = records_for(logged_rows(), "wide_stop")
    closed, opened = wide[0], wide[1]
    assert (closed["status"], closed["exit_reason"], closed["side"], closed["cohort"]) == ("closed", "stop", "long", 2)
    assert (closed["entry_price"], closed["exit_price"], closed["entry_score"]) == (100.0, 91.0, 7)
    assert (closed["stop_pct"], closed["size"], closed["hours_held"]) == (8.0, 1.0, 3.0)
    assert (closed["ma200"], closed["vol20_pct"], closed["event_flags"]) == (95.0, 2.0, "opec_day")
    assert closed["mfe_pct"] == pytest.approx(1.0)       # the 101 print
    assert closed["mae_pct"] == pytest.approx(-9.0)      # the 91 exit print
    assert closed["pnl_pct_sized"] == closed["pnl_pct"]
    assert opened["status"] == "open" and opened["exit_reason"] == "open" and opened["exit_utc"] == ""
    assert opened["entry_price"] == 92.0 and opened["exit_price"] == 92.0

    original = records_for(logged_rows(), "original")[0]
    assert (original["cohort"], original["exit_reason"], original["stop_pct"]) == (1, "stop", config.TRAILING_STOP_PCT)
    assert original["mae_pct"] == pytest.approx(-5.1)
    calendar = records_for(logged_rows(), "calendar")
    assert all(t["stop_pct"] == "" for t in calendar)


def test_the_log_records_the_stop_and_time_reasons_the_bots_use():
    rows = hold_rows(15, 9, 15, 12)
    reasons = {t["bot"]: t["exit_reason"] for t in leaderboard.bot_trades_report(
        [dict(r, context_version="c1") for r in rows])["trades"] if t["status"] == "closed"}
    assert reasons["short_hold"] == "time"


def test_bot_trades_json_and_tab_are_written(monkeypatch, tmp_path, capsys):
    client = FakeSheets({SIGNALS: logged_rows()})
    monkeypatch.setattr(leaderboard.sheets, "open_client", lambda dry_run=False: client)
    monkeypatch.chdir(tmp_path)
    assert leaderboard.main() == 0
    data = json.loads((tmp_path / "backup" / "bot_trades.json").read_text())
    assert data["columns"] == leaderboard.BOT_TRADE_COLUMNS and data["trades"]
    board = json.loads((tmp_path / "backup" / "leaderboard.json").read_text())
    assert board["cohort2"]["started"] and len(board["cohort2"]["bots"]) == 10
    assert "cohort 2" in capsys.readouterr().out
    tab = client.tables["bot_trades"]
    assert tab[0] == leaderboard.BOT_TRADE_COLUMNS
    assert len(tab) == len(data["trades"]) + 3 and tab[-2] == [] and tab[-1][0] == "updated_utc"
    assert "leaderboard" in client.tables


def test_the_leaderboard_tab_keeps_its_cohort_1_layout_and_adds_cohort_2_below():
    client = FakeSheets()
    leaderboard.write_tab(client, leaderboard.standings(logged_rows(), with_luck=False),
                          updated_utc="2026-11-25T00:00:00+00:00")
    rows = client.tables["leaderboard"]
    assert rows[0][0] == "rank" and rows[0][-1] == "cohort" and rows[1][0] == 1
    assert rows[7] == [] and rows[8][0] == "benchmark"
    assert rows[9] == ["updated_utc", "2026-11-25T00:00:00+00:00"]
    assert rows[10] == [] and rows[11][0] == "cohort 2" and rows[12][0] == "rank"
    labels = [r[1] for r in rows[13:24]]
    assert labels[-1] == "Bot 1 over the same window (reference, not a bot)"
    assert {l for l in labels[:-1]} == {b["label"] for b in leaderboard.NEW_BOTS}
    balances = [r[2] for r in rows[13:23]]
    assert balances == sorted(balances, reverse=True)
    assert rows[23][0] == "reference"


def test_existing_consumers_still_work_with_new_bots_registered():
    from scripts import build_page
    page = build_page.build([dict(r, rules_version="v2") for r in logged_rows()])
    for bot in leaderboard.BOTS:
        assert bot["label"] in page


# ------------------------------------------- bots 7 to 14 stay unchanged --

GOLDEN_7_TO_14 = os.path.join(HERE, "fixtures", "golden_bots_7_to_14.json")


def test_bots_7_to_14_and_the_reference_are_unchanged_by_adding_bots_15_and_16():
    """Expected values were generated from the code as committed before bots 15
    and 16 existed, over the pseudo random c1 dataset."""
    with open(GOLDEN_7_TO_14, encoding="utf-8") as handle:
        golden = json.load(handle)
    rows = long_run()
    keys = [b["key"] for b in leaderboard.NEW_BOTS[:8]] + [REF["key"]]
    assert sorted(golden["bots"]) == sorted(keys)
    for key in keys:
        bot = REF if key == REF["key"] else ALL[key]
        trades, position, info = leaderboard.run_bot_full(bot, rows)
        expected = golden["bots"][key]
        assert jsonable(trades) == expected["trades"], key
        assert jsonable(position_dict(position)) == expected["position"], key
        assert jsonable(info) == expected["info"], key
    section = leaderboard.standings(rows, with_luck=True)["cohort2"]
    entries = {b["key"]: b for b in section["bots"]}
    entries[REF["key"]] = section["reference"]
    for expected in golden["standings_cohort2_luck"]["bots"] + [golden["standings_cohort2_luck"]["reference"]]:
        assert jsonable(entries[expected["key"]]) == expected, expected["key"]


# ------------------------------------------------------------ Bot 15 -----

def test_trend_bar_enters_on_a_4_when_the_trend_agrees_and_bot_1_does_not():
    up = [row(14, 10, 100.0, 4, ma=95.0)]
    assert run("trend_bar", up)[1].side == "long"
    assert not run("original", up)[1].is_open
    down = [row(14, 10, 100.0, -4, ma=105.0)]
    assert run("trend_bar", down)[1].side == "short"
    assert not run("original", down)[1].is_open


def test_trend_bar_keeps_the_normal_bar_when_the_trend_disagrees_or_is_equal():
    assert not run("trend_bar", [row(14, 10, 100.0, 4, ma=105.0)])[1].is_open
    assert not run("trend_bar", [row(14, 10, 100.0, -4, ma=95.0)])[1].is_open
    assert not run("trend_bar", [row(14, 10, 100.0, 4, ma=100.0)])[1].is_open
    assert not run("trend_bar", [row(14, 10, 100.0, -4, ma=100.0)])[1].is_open
    assert not run("trend_bar", [row(14, 10, 100.0, 5, ma=105.0)])[1].is_open
    assert run("trend_bar", [row(14, 10, 100.0, 6, ma=105.0)])[1].side == "long"     # +6 whatever the trend
    assert run("trend_bar", [row(14, 10, 100.0, -6, ma=95.0)])[1].side == "short"
    assert not run("trend_bar", [row(14, 10, 100.0, 3, ma=95.0)])[1].is_open          # below even the low bar


def test_trend_bar_still_needs_confidence_and_the_friday_rule_and_exits_like_bot_1():
    assert not run("trend_bar", [row(14, 10, 100.0, 4, ma=95.0, confidence="low")])[1].is_open
    assert not run("trend_bar", [row(18, 11, 100.0, 4, ma=95.0)])[1].is_open         # Friday after 10:00
    rows = [row(14, 10, 100.0, 4, ma=95.0), row(14, 12, 101.0, -4, ma=95.0)]
    trades, _ = run("trend_bar", rows)
    assert [t["reason"] for t in trades] == ["reversal"]
    rows = [row(14, 10, 100.0, 4, ma=95.0), row(14, 12, 96.9, ma=95.0)]
    assert [t["reason"] for t in run("trend_bar", rows)[0]] == ["stop"]


def test_trend_bar_restores_config_always():
    saved = (config.ENTRY_LONG_SCORE, config.ENTRY_SHORT_SCORE, config.TRAILING_STOP_PCT)
    rows = [row(14, 10, 100.0, 4, ma=95.0), row(14, 11, 100.0, 4, ma=105.0)]
    run("trend_bar", rows)
    replay.replay_variant(rows, entry=4, trend_entry=3)
    assert (config.ENTRY_LONG_SCORE, config.ENTRY_SHORT_SCORE, config.TRAILING_STOP_PCT) == saved

    calls = []
    real = rules.decide

    def boom(**kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(**kw)

    rules_decide = rules.decide
    rules.decide = boom
    try:
        with pytest.raises(RuntimeError):
            replay.replay_variant(rows, trend_entry=4)
    finally:
        rules.decide = rules_decide
    assert (config.ENTRY_LONG_SCORE, config.ENTRY_SHORT_SCORE, config.TRAILING_STOP_PCT) == saved


def test_trend_bar_missing_ma200_behaves_like_bot_1_and_counts_only_decisions_it_could_change():
    rows = [row(14, 10, 100.0, 4)]                       # between the bars, ma200 never known
    trades, position, info = run_full("trend_bar", rows)
    assert not position.is_open and info["missing_input_decisions"] == 1
    assert info["missing_by_feature"] == {"ma200": 1}
    _, position, info = run_full("trend_bar", [row(14, 10, 100.0, 6)])
    assert position.side == "long" and info["missing_input_decisions"] == 0          # Bot 1 would enter anyway
    assert run_full("trend_bar", [row(14, 10, 100.0, 3)])[2]["missing_input_decisions"] == 0
    assert run_full("trend_bar", [row(14, 10, 100.0, 4, confidence="low")])[2]["missing_input_decisions"] == 0
    # carried forward: a known value from an earlier row is used
    rows = [row(14, 9, 100.0, ma=95.0), row(14, 10, 100.0, 4)]
    assert run("trend_bar", rows)[1].side == "long"


# ------------------------------------------------------------ Bot 16 -----

def test_trend_only_enters_with_no_score_and_ignores_scores_entirely():
    rows = [row(14, 10, 100.0, ma=95.0), row(14, 11, 100.0, ma=95.0)]
    assert run("trend_only", rows)[1].side == "long"
    assert run("trend_only", [row(14, 10, 100.0, 0, ma=95.0)])[1].side == "long"
    assert run("trend_only", [row(14, 10, 100.0, ma=105.0)])[1].side == "short"
    assert not run("trend_only", [row(14, 10, 100.0, ma=100.0)])[1].is_open

    def scenario(score_of):
        return [row(14, 10, 100.0, score_of(7), ma=95.0), row(14, 11, 94.0, score_of(-5), ma=95.0),
                row(14, 12, 93.0, score_of(8), ma=95.0), row(14, 13, 97.0, score_of(-9), ma=95.0),
                row(14, 14, 98.0, score_of(2), ma=95.0)]
    plain = run("trend_only", scenario(lambda s: None))
    assert run("trend_only", scenario(lambda s: s)) == plain
    assert run("trend_only", scenario(lambda s: -s)) == plain
    assert len(plain[0]) == 2


def test_trend_only_flips_with_trend_flip_and_enters_the_new_side_on_a_later_row():
    rows = [row(14, 10, 100.0, ma=95.0), row(14, 11, 94.0, ma=95.0), row(14, 12, 93.0, ma=95.0)]
    trades, position = run("trend_only", rows)
    assert [(t["side"], t["reason"], t["exit_price"]) for t in trades] == [("long", "trend_flip", 94.0)]
    assert position.side == "short" and position.entry_price == 93.0
    assert not run("trend_only", rows[:2])[1].is_open      # flat on the flip row itself
    assert trades[0]["pnl_usd"] == pytest.approx(-6.0 - config.SPREAD_USD_PER_BBL)
    assert set(trades[0]) == set(run("original", [row(14, 10, 100.0, 7), row(14, 12, 101.0, -5)])[0][0])
    equal = rows[:1] + [row(14, 11, 95.0, ma=95.0)]
    assert run("trend_only", equal)[1].is_open             # equal is not on the other side


def test_trend_only_closes_on_friday_and_takes_no_new_entry_late_on_friday():
    rows = [row(17, 10, 100.0, ma=95.0), row(18, 15, 101.0, minute=5, ma=95.0)]
    trades, position = run("trend_only", rows)
    assert [t["reason"] for t in trades] == ["friday"] and not position.is_open
    assert not run("trend_only", [row(18, 11, 100.0, ma=95.0)])[1].is_open
    late = [row(17, 10, 100.0, ma=95.0), row(19, 12, 101.0, ma=95.0)]    # Saturday: the late weekend close
    assert [t["reason"] for t in run("trend_only", late)[0]] == ["friday"]


def test_trend_only_stays_flat_with_unknown_ma200_and_counts_the_rows():
    rows = [row(14, 10, 100.0, ctx=False), row(14, 11, 101.0, ctx=False), row(14, 12, None, ctx=False)]
    rows[2]["brent_price"] = ""
    trades, position, info = run_full("trend_only", rows)
    assert trades == [] and not position.is_open
    assert info["missing_input_decisions"] == 2 and info["missing_by_feature"] == {"ma200": 2}
    carried = [row(14, 10, 100.0, ma=95.0), row(14, 11, 101.0)]
    assert run_full("trend_only", carried)[2]["missing_input_decisions"] == 0


def test_trend_only_has_no_luck_p_and_the_shuffle_never_runs_for_it(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the shuffle must not run for a bot that reads no news")

    monkeypatch.setattr(replay, "shuffled_strategy", boom)
    bot = ALL["trend_only"]
    rows = long_run()
    trades, _ = leaderboard.run_bot(bot, rows)
    assert len(trades) >= leaderboard.MIN_TRADES_FOR_LUCK
    assert leaderboard._luck(bot, rows, trades, 1.0, True) == (None, "n/a, reads no news")


def test_trend_only_is_logged_with_mfe_and_mae_and_no_stop():
    rows = [row(14, 10, 100.0, ma=95.0, flags="opec_day"), row(14, 11, 103.0, ma=95.0), row(14, 12, 94.0, ma=95.0)]
    records = records_for(rows, "trend_only")
    first = records[0]
    assert (first["exit_reason"], first["cohort"], first["stop_pct"], first["event_flags"]) == ("trend_flip", 2, "", "opec_day")
    assert first["mfe_pct"] == pytest.approx(3.0) and first["mae_pct"] == pytest.approx(-6.0)


# ---------------------------------------- cohort 2 with bots 15 and 16 ---

def test_cohort_2_standings_include_bots_15_and_16():
    section = leaderboard.standings(long_run(), with_luck=False)["cohort2"]
    keys = [b["key"] for b in section["bots"]]
    assert keys[-2:] == ["trend_bar", "trend_only"] and len(keys) == 10
    by_key = {b["key"]: b for b in section["bots"]}
    assert by_key["trend_bar"]["trades"] > 0 and by_key["trend_only"]["trades"] > 0
    assert by_key["trend_only"]["luck_note"] == "n/a, reads no news"
    assert section["reference"]["key"] == "original_same_window"
    table = leaderboard.standings(long_run(), with_luck=False)
    assert len(table["bots"]) == 6
    report = leaderboard.bot_trades_report(long_run())
    assert {"trend_bar", "trend_only"} <= {t["bot"] for t in report["trades"]}
