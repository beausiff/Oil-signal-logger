"""No test may touch the network through the context feeds.

src/main.py gathers market context on every run (src/context.py). Without this,
each test that calls main.run would make real FRED, EIA and CFTC requests. Every
feed is made to fail instantly instead; tests/test_context.py swaps in its own.
"""

import pytest

from src import context


@pytest.fixture(autouse=True)
def offline_context(monkeypatch):
    def offline(now_utc, budget_seconds=context.TOTAL_BUDGET_SECONDS):
        return {name: context.FeedResult(False, None, "offline_in_tests")
                for name in context.feed_jobs(now_utc)}

    offline.real = context.fetch_all  # for the one test of the real thing
    monkeypatch.setattr(context, "fetch_all", offline)
