from datetime import datetime, timedelta, timezone

from src import news


def headline(title, source="Reuters", minutes_ago=10):
    published = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return news.Headline(
        hash=news.headline_hash(title, source),
        published_utc=published.isoformat(),
        source=source,
        title=title,
        url="https://example.com/%s" % abs(hash(title)),
    )


def test_normalise_strips_the_source_suffix_and_punctuation():
    assert news.normalise_title("Brent jumps 3% - Reuters") == "brent jumps 3"


def test_same_story_from_the_same_source_hashes_the_same():
    a = news.headline_hash("OPEC+ holds output steady", "Reuters")
    b = news.headline_hash("OPEC+ holds output steady!", "reuters")
    assert a == b


def test_different_sources_hash_differently():
    a = news.headline_hash("OPEC holds output steady", "Reuters")
    b = news.headline_hash("OPEC holds output steady", "Bloomberg")
    assert a != b


def test_dedupe_drops_known_hashes():
    first = headline("Tanker struck in the Red Sea")
    second = headline("Talks resume in Vienna")
    kept = news.filter_and_dedupe([first, second], [first.hash], datetime.now(timezone.utc))
    assert [h.title for h in kept] == ["Talks resume in Vienna"]


def test_running_twice_yields_nothing_the_second_time():
    batch = [headline("Tanker struck in the Red Sea"), headline("Talks resume in Vienna")]
    now = datetime.now(timezone.utc)
    first_pass = news.filter_and_dedupe(batch, [], now)
    assert len(first_pass) == 2
    second_pass = news.filter_and_dedupe(batch, [h.hash for h in first_pass], now)
    assert second_pass == []


def test_dedupe_within_one_batch():
    duplicate = headline("Hormuz transit halted")
    kept = news.filter_and_dedupe([duplicate, duplicate], [], datetime.now(timezone.utc))
    assert len(kept) == 1


def test_old_headlines_are_dropped():
    kept = news.filter_and_dedupe(
        [headline("Stale story", minutes_ago=200)], [], datetime.now(timezone.utc)
    )
    assert kept == []


def test_most_recent_first_and_capped():
    now = datetime.now(timezone.utc)
    batch = [headline("Story %d" % i, minutes_ago=i) for i in range(1, 60)]
    kept = news.filter_and_dedupe(batch, [], now, cap=40)
    assert len(kept) == 40
    assert kept[0].title == "Story 1"


def test_query_covers_every_search_term():
    query = news.build_query()
    for term in ("oil", "Brent", "OPEC", "Hormuz", "Red Sea"):
        assert term in query
    assert query.endswith("when:1d")
