import pytest

from scripts import pilot_archive_vs_live as p

TERMS = ["oil", "Brent", "Red Sea", "Iran"]


def test_normalise_title():
    assert p.normalise_title("Rise in prices could push inflation to a 3 - year high") == \
        "Rise in prices could push inflation to a 3-year high"
    assert p.normalise_title("Oil falls , Brent steadies") == "Oil falls, Brent steadies"
    assert p.normalise_title("Trump ' s plan") == "Trump's plan"
    assert p.normalise_title("Iran War 2026 - LIVE UPDATES") == "Iran War 2026 - LIVE UPDATES"


def test_filter_titles_whole_word_dedupe_newest():
    rows = [
        {"published_utc": "2026-10-01T10:00:00", "title": "Oil jumps on Iran news"},
        {"published_utc": "2026-10-01T11:00:00", "title": "Recipe : baked rice"},
        {"published_utc": "2026-10-01T12:00:00", "title": "Oil jumps on Iran news"},   # exact duplicate
        {"published_utc": "2026-10-01T09:00:00", "title": "Soil samples improve"},      # 'oil' not a whole word
        {"published_utc": "2026-10-01T08:00:00", "title": "Red Sea shipping resumes"},
        {"published_utc": "2026-10-01T07:00:00", "title": "brent slips"},                # case insensitive
    ]
    out = p.filter_titles(rows, TERMS)
    assert [r["title"] for r in out] == ["Oil jumps on Iran news", "Red Sea shipping resumes", "brent slips"]
    assert len(p.filter_titles(rows * 1, TERMS, keep=2)) == 2
    assert out[0]["published_utc"] == "2026-10-01T12:00:00"  # newest copy kept


def test_jaccard_and_overlap():
    assert p.jaccard("Oil prices surge on Iran fears", "Oil prices surge on Iran fears") == 1.0
    assert p.jaccard("alpha bravo charlie delta", "echo foxtrot golf hotel") == 0.0
    live = ["Oil prices surge on Iran fears today", "Completely different headline here"]
    arc = ["Oil prices surge on Iran fears today", "Something else entirely unrelated"]
    assert p.title_overlap(live, arc) == 0.5
    assert p.title_overlap([], arc) is None


def test_verdict_boundaries():
    assert p.verdict(0.60, 0.70, 30) == "GOOD ENOUGH"
    assert p.verdict(0.60, 0.699, 30) == "BORDERLINE"
    assert p.verdict(0.599, 0.95, 30) == "BORDERLINE"
    assert p.verdict(0.40, 0.10, 30) == "BORDERLINE"
    assert p.verdict(0.399, 0.99, 30) == "NOT GOOD ENOUGH"
    assert p.verdict(None, 1.0, 30).startswith("NO VERDICT")
    assert p.verdict(0.9, 1.0, 3).startswith("NO VERDICT")


def test_spend_cap_refuses_61st_call(tmp_path):
    path = str(tmp_path / "calls.txt")
    cap = p.SpendCap(path=path, max_calls=60)
    for _ in range(60):
        cap.consume()
    with pytest.raises(p.SpendCapExceeded):
        cap.consume()
    assert p.SpendCap(path=path, max_calls=60).used == 60          # persists across runs
    with pytest.raises(p.SpendCapExceeded):
        p.SpendCap(path=path, max_calls=60).consume()


def test_counting_client_blocks_inner_call(tmp_path):
    calls = []

    class Inner:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(1)
                return "ok"

    cap = p.SpendCap(path=str(tmp_path / "c.txt"), max_calls=2)
    client = p.CountingClient(Inner, cap)
    assert client.messages.create() == "ok" and client.messages.create() == "ok"
    with pytest.raises(p.SpendCapExceeded):
        client.messages.create()
    assert len(calls) == 2


def test_evenly_and_eligible():
    assert len(p.evenly(list(range(82)), 56)) == 56
    assert p.evenly(list(range(82)), 56)[0] == 0 and p.evenly(list(range(82)), 56)[-1] == 81
    ok = {"run_utc": "2026-10-01T10:00:00+00:00", "score": "3", "score_source": "live", "new_headline_count": "6"}
    assert p.eligible(ok)
    assert not p.eligible({**ok, "score_source": "carried"})
    assert not p.eligible({**ok, "score_source": "backfilled:2026-11-05"})
    assert not p.eligible({**ok, "new_headline_count": "4"})
    assert not p.eligible({**ok, "score": ""})
    assert not p.eligible({**ok, "run_utc": "2026-10-17T00:00:00+00:00"})


def test_parse_gdelt_csv():
    body = "﻿url,mobileurl,date,title\nhttp://a.com/x,,2026-10-01 10:00:00,Oil rises\n"
    rows = p.parse_gdelt_csv(body)
    assert rows[0]["title"] == "Oil rises" and rows[0]["published_utc"].startswith("2026-10-01T10:00:00")
    assert p.parse_gdelt_csv("No results") == []
