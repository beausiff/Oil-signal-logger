"""The race page generator, on small synthetic rows, with real code."""

import re
from datetime import datetime, timezone

from scripts import build_page, leaderboard
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
