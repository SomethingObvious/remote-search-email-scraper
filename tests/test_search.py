import time
from unittest.mock import patch

import pytest
from conftest import fake_web, load
from ddgs.exceptions import DDGSException, RatelimitException

from remotesearch import search
from remotesearch.net import SourceError
from remotesearch.search import Hit, brave, duckduckgo, snippet_answer, web_results

BRAVE = "https://api.search.brave.com/res/v1/web/search"


def test_duckduckgo_turns_ddgs_rows_into_hits(ddg) -> None:
    hits = duckduckgo("how long to boil water")
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


def test_duckduckgo_backs_off_then_gives_up(ddg) -> None:
    ddg.errors = [DDGSException("No results found."), RatelimitException("202")] * 2
    with patch("time.sleep") as sleep, pytest.raises(SourceError) as caught:
        duckduckgo("boil water")
    assert [c.args[0] for c in sleep.call_args_list] == [0, 2, 5]
    assert (caught.value.name, caught.value.why) == ("DuckDuckGo", "it's limiting requests")
    assert len(ddg.queries) == 3


def test_duckduckgo_recovers_on_a_retry(ddg) -> None:
    ddg.errors = [DDGSException("No results found.")]
    assert len(duckduckgo("boil water")) == 3
    assert len(ddg.queries) == 2


def test_duckduckgo_cools_down_after_a_block(ddg) -> None:
    ddg.errors = [DDGSException("No results found.")] * 3
    with pytest.raises(SourceError):
        duckduckgo("first")
    with pytest.raises(SourceError):
        duckduckgo("second")  # refused straight away, without asking again
    assert ddg.queries == ["first"] * 3
    search._ddg_blocked_until = time.monotonic() - 1
    assert len(duckduckgo("third")) == 3


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


def test_snippet_answer_needs_a_relevant_hit() -> None:
    hits = [
        Hit("Vibram S.p.A.", "An Italian company.", "https://a"),
        Hit("Blister first aid", "Cover it with moleskin.", "https://b"),
    ]
    assert snippet_answer("treat a blister", hits) == "Blister first aid: Cover it with moleskin."
    assert snippet_answer("best tent stakes", hits) is None
    assert snippet_answer("blister", [Hit("Blister.", "", "https://c")]) == "Blister"
