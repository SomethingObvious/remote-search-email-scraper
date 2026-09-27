import time
from unittest.mock import patch

import pytest
from conftest import fake_web, load
from ddgs.exceptions import DDGSException, RatelimitException

from remotesearch import search
from remotesearch.net import SourceError
from remotesearch.search import Hit, brave, ddgs_search, snippet_answer, web_results

BRAVE = "https://api.search.brave.com/res/v1/web/search"


def test_ddgs_search_turns_rows_into_hits(ddg) -> None:
    hits = ddgs_search("how long to boil water")
    assert hits[0] == Hit(
        "How Long to Boil Water to Purify for Drinking (According to Science)",
        "Whether you're hiking or camping this summer, safe water is pretty important. In this "
        "post, you'll learn how long to boil water to make it safe for drinking. Plus, you'll "
        "learn about what makes you sick, other purification methods, and we'll answer many "
        "questions about safe drinking water.",
        "https://storyteller.travel/how-long-to-boil-water/",
    )
    assert len(hits) == 3
    assert ddg.queries == ["how long to boil water"]
    assert ddg.backends == ["duckduckgo"]


def test_blocked_duckduckgo_falls_back(ddg) -> None:
    ddg.blocked = {"duckduckgo"}
    ddg.errors = [RatelimitException("202")]  # and Brave's result page turns it away once
    assert len(ddgs_search("first")) == 3
    assert ddg.backends == ["duckduckgo", "brave", "mojeek"]
    # The two that failed are left alone for a while instead of being asked on every text.
    assert len(ddgs_search("second")) == 3
    assert ddg.backends[3:] == ["mojeek"]
    search._resting.update(dict.fromkeys(search._resting, time.monotonic() - 1))
    ddg.blocked = set()
    ddgs_search("third")
    assert ddg.backends[4:] == ["duckduckgo"]


def test_every_engine_failing_backs_off_then_gives_up(ddg) -> None:
    ddg.blocked = set(search.BACKENDS)
    with patch("time.sleep") as sleep, pytest.raises(SourceError) as caught:
        ddgs_search("boil water")
    assert [c.args[0] for c in sleep.call_args_list] == [0, 2, 5]
    assert (caught.value.name, caught.value.why) == ("the web search", "it's limiting requests")
    assert ddg.backends == list(search.BACKENDS) * 3
    with pytest.raises(SourceError):
        ddgs_search("second")  # refused straight away, without asking again
    assert len(ddg.queries) == 3 * len(search.BACKENDS)


def test_ddgs_search_recovers_on_a_retry(ddg) -> None:
    ddg.errors = [DDGSException("No results found.")] * len(search.BACKENDS)
    assert len(ddgs_search("boil water")) == 3
    assert ddg.backends == [*search.BACKENDS, "duckduckgo"]
    assert search._resting == {}  # DuckDuckGo answered in the end, so nothing sits out


def test_brave_reads_the_results() -> None:
    with fake_web({BRAVE: load("made_brave_web.json")}) as calls:
        hits = brave("how long to boil water", "BSAkey")
    assert hits == [
        Hit(
            "Making Water Safe in an Emergency | Water, Sanitation, and Environmentally Related "
            "Hygiene",
            "Boil water for 1 minute. At elevations above 6,500 feet, boil water for 3 minutes.",
            "https://www.cdc.gov/water-emergency/about/index.html",
        ),
        Hit(
            "Boil water advisory",
            "Bring water to a rolling boil for at least one minute.",
            "https://www.canada.ca/en/health-canada/services/environmental-workplace-health/"
            "water-quality/drinking-water/boil-water-advisory.html",
        ),
    ]
    assert calls == [(BRAVE, {"q": "how long to boil water", "count": 5})]


def test_brave_sends_its_key_and_skips_bad_rows() -> None:
    seen = {}

    def get_json(url, name, **kwargs):
        seen.update(kwargs["headers"])
        return {"web": {"results": [{"title": "no link"}, "junk", {"title": "ok", "url": "u"}]}}

    with patch("remotesearch.net.get_json", get_json):
        assert brave("q", "BSAkey") == [Hit("ok", "", "u")]
    assert seen == {"Accept": "application/json", "X-Subscription-Token": "BSAkey"}
    with fake_web({BRAVE: {"type": "search"}}):
        assert brave("q", "BSAkey") == []


def test_web_results_uses_brave_only_with_a_key(ddg) -> None:
    with fake_web({BRAVE: load("made_brave_web.json")}):
        assert web_results("boil water", "BSAkey")[0].url.startswith("https://www.cdc.gov/")
    assert ddg.queries == []
    assert web_results("boil water")[0].url == "https://storyteller.travel/how-long-to-boil-water/"


def test_web_results_falls_back_when_brave_fails(ddg) -> None:
    with fake_web({BRAVE: SourceError("Brave Search", "it's limiting requests")}):
        hits = web_results("boil water", "BSAkey")
    assert hits[0].url == "https://storyteller.travel/how-long-to-boil-water/"
    assert ddg.backends == ["duckduckgo"]


def test_snippet_answer_needs_a_relevant_hit() -> None:
    hits = [
        Hit("Vibram S.p.A.", "An Italian company.", "https://a"),
        Hit("Blister first aid", "Cover it with moleskin.", "https://b"),
    ]
    assert snippet_answer("treat a blister", hits) == "Blister first aid: Cover it with moleskin."
    assert snippet_answer("best tent stakes", hits) is None
    assert snippet_answer("blister", [Hit("Blister.", "", "https://c")]) == "Blister"
