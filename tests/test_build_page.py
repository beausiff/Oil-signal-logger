"""The race page generator, on small synthetic rows, with real code."""

import re
from datetime import datetime, timezone

from scripts import backfill_context, build_page, leaderboard
from src import rules


def row(year, month, day, hour, price, score="", conf="", version="v1.1", minute=0, **extra):
    local = datetime(year, month, day, hour, minute, tzinfo=rules.CHICAGO)
    base = {
        "run_chicago": local.isoformat(),
        "run_utc": local.astimezone(timezone.utc).isoformat(),
        "rules_version": version, "brent_price": str(price), "score": str(score),
        "confidence": conf, "key_headlines": "", "reasoning": "", "score_source": "live",
    }
    base.update(extra)
    return base


def week(version, day0, headline="Tanker hit", reasoning="Escalation"):
    """Mon entry at +7, Tue hold, Friday close run: one winning long."""
    return [
        row(2026, 9, day0, 10, 100.0, 7, "high", version, key_headlines=headline, reasoning=reasoning),
        row(2026, 9, day0 + 1, 10, 101.0, 1, "high", version),
        row(2026, 9, day0 + 4, 15, 103.0, 0, "high", version, minute=5),
    ]


ARCHIVE = week("v1.1", 7)
RACE = week("v2", 14)


def section(page, name):
    start = page.index('<section id="%s">' % name)
    return page[start:page.index("</section>", start)]


def test_fragment_shape_and_forbidden_characters():
    page = build_page.build(ARCHIVE + RACE)
    assert page.startswith("<title>Oil Bot Race</title>")
    for banned in ("<html", "<body", "<!DOCTYPE", "—", "–"):
        assert banned not in page
    # The only script is the refresh button, which fires the refresh task.
    assert page.count("<script") == 1
    assert build_page.REFRESH_TRIGGER_ID in page


def test_how_it_runs_names_the_oanda_mirror():
    page = build_page.build(ARCHIVE + RACE)
    assert "No broker" not in page
    assert "OANDA practice account" in page


def test_all_six_bots_in_scoreboard_and_cards():
    page = build_page.build(ARCHIVE + RACE)
    for bot in leaderboard.BOTS:
        assert bot["label"] in section(page, "scoreboard")
        assert bot["label"] in section(page, "bots")


def test_no_race_rows_says_so_and_still_renders_the_archive():
    page = build_page.build(ARCHIVE)
    assert "has not started" in section(page, "scoreboard")
    assert "No trades yet." in section(page, "bots")
    assert "What we learned" in section(page, "archive")
    assert "Bot 1 on the first ten weeks" in page
    assert "Actually logged" in page


def test_data_text_is_escaped():
    rows = week("v1.1", 7, headline="<b>bold</b> move", reasoning="<i>x</i>") + RACE
    page = build_page.build(rows)
    assert "&lt;b&gt;bold&lt;/b&gt;" in page
    assert "<b>bold</b>" not in page and "<i>x</i>" not in page


def test_same_input_gives_identical_output():
    assert build_page.build(ARCHIVE + RACE) == build_page.build(list(reversed(ARCHIVE + RACE)))


def test_simulate_flag_is_marked_and_default_is_not():
    rows = ARCHIVE + week("v1.1", 14)
    assert "SIMULATED" in build_page.build(rows, simulate_race=3)
    assert "SIMULATED" not in build_page.build(rows)
    assert "has not started" in build_page.build(rows)


def test_balance_wording_and_no_per_barrel_in_scoreboard_cards_or_archive_table():
    page = build_page.build(ARCHIVE + RACE)
    assert "$100 is now" in section(page, "scoreboard")
    bots = section(page, "bots")
    assert bots.count("$100 is now") == 6
    archive = section(page, "archive")
    table = archive[archive.index("<table>"):archive.index("</table>")]
    for text in (section(page, "scoreboard"), bots, table):
        assert "per barrel" not in text
    assert "$100 became" in table
    assert "Profit per barrel" not in page


def test_negative_money_has_the_minus_before_the_dollar_sign():
    assert build_page.money(-3.2) == "\u2212$3.20"
    assert build_page.money(-3.2, signed=False) == "\u2212$3.20"
    assert build_page.money(104.123, signed=False) == "$104.12"
    assert "$-" not in build_page.build(ARCHIVE + RACE)


# ---------------------------------------------------------------- cohort 2 --

import csv
import os

from scripts import cohort2_page, feed_b_page

REAL_CSV = os.path.join(os.path.dirname(__file__), "..", "backup", "signals.csv")


def _v2_rows():
    """Synthetic rows under v2 with context. Row 0 enters long on a +7 and a high
    volatility, row 2 is 25 hours later (time exit for the 24 hour bots), then a
    price move through the 200 day average flips the trend bot."""
    rows = []
    spec = [  # (day, hour, price, score, ma200, vol)
        (14, 10, 100.0, 7, 95, 4.5), (14, 16, 101.0, 2, 95, 4.5), (15, 12, 102.0, 1, 95, 4.5),
        (15, 18, 90.0, 0, 95, 4.5), (16, 10, 91.0, -7, 95, 4.5), (16, 15, 93.0, 0, 95, 4.5),
        (17, 10, 94.0, 0, 95, 4.5), (18, 10, 96.0, 0, 95, 4.5), (18, 15, 97.0, 0, 95, 4.5),
    ]
    for day, hour, price, score, ma, vol in spec:
        rows.append(row(2026, 9, day, hour, price, score, "high", "v2", key_headlines="Tanker hit",
                        reasoning="Escalation at sea", context_version="c1", ma200=str(ma),
                        vol20_pct=str(vol), event_flags=""))
    return rows


def test_every_new_bot_has_rule_text_and_unknown_key_does_not_raise():
    for bot in leaderboard.NEW_BOTS:
        assert cohort2_page.rule_text(bot["key"], build_page.MINUS).strip(), bot["key"]
    assert cohort2_page.rule_text("no_such_bot", build_page.MINUS) == ""
    assert build_page.MINUS + "4" in cohort2_page.rule_text("trend_bar", build_page.MINUS)


def test_reason_text_covers_the_new_exits():
    assert build_page.REASON_TEXT["time"] == "24 hour limit"
    assert build_page.REASON_TEXT["trend_flip"] == "trend flipped"


def _page_without_new_section(page):
    start = page.index('<section id="newbots">')
    end = page.index("</section>", start) + len("</section>\n")
    return page[:start] + page[end:], page[start:end]


def test_real_csv_without_context_is_backfilled_and_ranks_all_16_bots():
    with open(REAL_CSV, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    # Recreate the file as it was before the logger wrote context: no context columns.
    for row in rows:
        for name in [k for k in row if k.startswith("context_") or k.startswith("ctx_")
                     or k in ("vol20_pct", "ma200", "event_flags")]:
            del row[name]
    page = build_page.build(rows)
    board = section(page, "scoreboard")
    for bot in leaderboard.ALL_BOTS:
        assert bot["label"] in board
    assert "replayed" in board
    new = section(page, "newbots")
    assert new.count("<article") == 10 and "<table>" not in new
    assert page.index('<section id="bots">') < page.index('<section id="newbots">') < page.index('<section id="feedb">')


def test_backfill_uses_nothing_after_each_row_and_keeps_logged_context():
    rows = ARCHIVE + _v2_rows()
    for r in rows[len(ARCHIVE):len(ARCHIVE) + 4]:
        for k in ("context_version", "ma200", "vol20_pct", "event_flags"):
            r[k] = ""
    filled = backfill_context.backfill(rows)
    race = leaderboard.race_rows(filled)
    assert [r["context_version"] for r in race[:4]] == [backfill_context.REPLAYED] * 4
    assert all(r["context_version"] == "c1" and r["ma200"] == "95" for r in race[4:])
    # Changing a later price never changes an earlier filled row.
    later = [dict(r) for r in rows]
    later[-1]["brent_price"] = "150.0"
    again = leaderboard.race_rows(backfill_context.backfill(later))
    assert [(r["ma200"], r["vol20_pct"]) for r in again[:4]] == [(r["ma200"], r["vol20_pct"]) for r in race[:4]]


def test_started_cohort_on_the_same_hours_joins_the_top_scoreboard():
    rows = ARCHIVE + _v2_rows()
    page = build_page.build(rows)
    table = leaderboard.standings(rows, with_luck=False)
    ranked = build_page.all_bots_ranked(table)
    assert len(ranked) == 16
    board = section(page, "scoreboard")
    positions = [board.index(">%s<" % b["label"]) for b in ranked]
    assert positions == sorted(positions)
    for b in ranked:
        assert build_page.money(b["balance_now"], signed=False) in board
    assert board.count('class="lead"') <= 1
    assert "With 16 bots" in board
    new = section(page, "newbots")
    assert new.count("<article") == 10 and "With 16 bots racing" in new


def test_sized_stop_and_exit_reasons_are_shown():
    rows = ARCHIVE + _v2_rows()
    new = section(build_page.build(rows), "newbots")
    race = leaderboard.cohort2_rows(rows)
    sized = leaderboard.run_bot(leaderboard.NEW_BOTS[6], race)[0]       # vol_size
    assert sized and "size" in sized[0]
    assert "size %.2f" % sized[0]["size"] in new
    stopped = leaderboard.run_bot(leaderboard.NEW_BOTS[0], race)[0] + leaderboard.run_bot(leaderboard.NEW_BOTS[1], race)[0]
    assert any("stop_pct" in t for t in stopped)
    assert "stop 8.0%" in new
    reasons = {t["reason"] for bot in leaderboard.NEW_BOTS for t in leaderboard.run_bot(bot, race)[0]}
    assert {"time", "trend_flip"} <= reasons
    assert "24 hour limit" in new and "trend flipped" in new
    # The sized bot's running balance uses sized returns.
    first = sized[0]
    expected = 100.0 * (1 + leaderboard.sized_pct(first) / 100.0)
    assert "balance %s" % build_page.money(expected, signed=False) in new


def test_negated_bot_says_so_and_news_free_bots_have_no_quote():
    new = section(build_page.build(ARCHIVE + _v2_rows()), "newbots")
    cards = new.split("<article")[1:]
    fade = [c for c in cards if "Bot 10" in c][0]
    assert "this bot traded against this view" in fade
    trend = [c for c in cards if "Bot 16" in c][0]
    assert "The model, on entry" not in trend


def test_missing_input_sentence_only_when_decisions_were_skipped():
    new = section(build_page.build(ARCHIVE + _v2_rows()), "newbots")
    assert "without trend or volatility data" not in new
    rows = [dict(r, ma200="", vol20_pct="") for r in _v2_rows()]
    c2 = leaderboard.standings(ARCHIVE + rows, with_luck=False)["cohort2"]
    total = sum(b["missing_input_decisions"] for b in c2["bots"])
    assert total > 0
    new = section(build_page.build(ARCHIVE + rows), "newbots")
    assert "%d decisions in total were made without trend or volatility data" % total in new


def test_started_page_keeps_the_forbidden_characters_out_and_is_deterministic():
    rows = ARCHIVE + _v2_rows()
    page = build_page.build(rows)
    for banned in ("<html", "<body", "<!DOCTYPE", "—", "–"):
        assert banned not in page
    assert page.count("<script") == 1
    assert "$-" not in page
    assert page == build_page.build(list(reversed(rows)))
    assert page == build_page.build(rows)


def test_data_text_in_new_cards_is_escaped():
    rows = [dict(r, reasoning="<i>x</i>") for r in _v2_rows()]
    new = section(build_page.build(ARCHIVE + rows), "newbots")
    assert "&lt;i&gt;x&lt;/i&gt;" in new and "<i>x</i>" not in new
