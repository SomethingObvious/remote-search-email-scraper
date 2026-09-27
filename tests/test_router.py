import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import DDG, OPEN_METEO, OPEN_METEO_GEOCODE, WIKI_SEARCH, fake_web, fixture_text, load
from ddgs.exceptions import DDGSException

from remotesearch import ai
from remotesearch.net import SourceError
from remotesearch.router import (
    EMPTY_TEXT,
    HELP_TEXT,
    NOTHING_MORE_TEXT,
    Answerer,
    Responder,
    answer,
)
from remotesearch.state import State
from remotesearch.text import GSM7_BASIC, GSM7_EXTENSION, MAX_QUERY_CHARS, MORE, sms_segments

DICTIONARY = "https://api.dictionaryapi.dev/"
ALBEDO = [
    {
        "meanings": [
            {"partOfSpeech": "noun", "definitions": [{"definition": "Reflectivity."}]},
            {"partOfSpeech": "verb", "definitions": [{"definition": "To reflect."}]},
        ]
    }
]
AI_CONFIG = {"AI_API_KEY": "sk-proj-abc"}


def test_every_help_fits_one_sms() -> None:
    assert len(HELP_TEXT) <= 160
    assert sms_segments(HELP_TEXT) == 1
    for word, command in Answerer().commands.items():
        assert len(command.help) <= 160, word
        assert sms_segments(command.help) == 1, word
        assert command.help.startswith(command.words[0]), word


def test_help_words_and_help_for_one_command(ddg) -> None:
    for word in ("help", "HELP", "?", "commands"):
        assert answer(word) == HELP_TEXT
    assert answer("help tide").startswith("tide <place>: the next highs and lows")
    assert answer("help tides") == answer("help tide")
    assert answer("help road") == answer("help roads")


def test_help_me_is_a_real_question(ddg) -> None:
    with fake_web({}):
        reply = answer("help me boil water")
    assert reply.startswith("web: How Long to Boil Water")
    assert ddg.queries == ["help me boil water"]


def test_empty_text() -> None:
    assert answer("") == EMPTY_TEXT
    assert answer("   \n ") == EMPTY_TEXT


def test_long_text_is_capped_before_any_lookup(ddg) -> None:
    with fake_web({}) as calls:
        reply = answer("help " + "x" * 5000, limit=300)
    assert len(reply) <= 300
    assert calls, "the long text should still have gone to the web search"
    for _url, params in calls:
        for value in params.values():
            assert len(str(value)) <= MAX_QUERY_CHARS
    assert all(len(q) <= MAX_QUERY_CHARS for q in ddg.queries)


def test_the_command_that_answered_tags_the_reply() -> None:
    with fake_web({DICTIONARY: ALBEDO}):
        assert answer("define albedo") == "define: (noun) Reflectivity. (verb) To reflect."
        assert answer("Def: albedo") == "define: (noun) Reflectivity. (verb) To reflect."


def test_fallback_skips_the_source_it_tried(ddg) -> None:
    ddg.rows = []
    with fake_web({WIKI_SEARCH: {"query": {"search": []}}}) as calls:
        answer("wiki zzqx")
    urls = [url for url, _ in calls]
    assert urls.count(WIKI_SEARCH) == 1
    assert urls.count(DDG) == 1
    assert ddg.queries == ["zzqx"]


def test_the_fallback_folds_what_gsm7_cant_send(ddg) -> None:
    ddg.rows = []
    with fake_web({DDG: {"AbstractText": "Forêt is forest in French, and café is café."}}):
        assert answer("wiki foret") == "web: Foret is forest in French, and café is café."


def test_no_result_reply_keeps_the_advice(ddg) -> None:
    ddg.rows = []
    question = (
        "how do I treat a blister on a long hike when there is no pharmacy anywhere near "
        "and the only things in the pack are tape, a lighter, a knife, some moleskin and "
        "a very small first aid kit that was mostly used up"
    )
    with fake_web({}):
        reply = answer(question)
    assert reply.startswith("Couldn't find a good answer for 'how do I treat a blister")
    assert reply.endswith("or text help for the commands.")
    assert len(reply) <= 300


def test_words_that_start_sentences_need_a_real_argument(ddg) -> None:
    ddg.rows = []
    so = "https://api.stackexchange.com/"
    with fake_web({so: {"items": []}}) as calls:
        answer("so what is lye")
        answer("time is money")
        answer("score of the game")
        answer("hours of daylight in june")
    assert not any(url.startswith((so, OPEN_METEO_GEOCODE)) for url, _ in calls)
    assert ddg.queries == [
        "so what is lye",
        "time is money",
        "score of the game",
        "hours of daylight in june",
    ]
    with fake_web({so: {"items": []}}) as calls:
        answer("so python yield")
    assert calls[0][0] == "https://api.stackexchange.com/2.3/search/advanced"


def test_a_leading_in_doesnt_stop_a_place_command() -> None:
    with fake_web({OPEN_METEO_GEOCODE: load("open_meteo_geocode_tofino.json")}):
        assert answer("time in Tofino").startswith("time: Tofino, British Columbia, CA: ")


def test_news_works_on_its_own() -> None:
    with fake_web({"https://news.google.com/rss": fixture_text("google_news_wildfire.xml")}):
        assert answer("news").startswith("news: 1) 'God cleaned the slate'")


def test_a_command_whose_source_is_down_says_so() -> None:
    with fake_web({"https://api.open511.gov.bc.ca/": SourceError("DriveBC", "it timed out")}):
        assert answer("road hwy 99") == (
            "DriveBC isn't answering right now (it timed out). Try again in a few minutes."
        )
    with fake_web({DICTIONARY: SourceError("the dictionary", "it answered with error 522")}):
        assert answer("define albedo") == (
            "The dictionary isn't answering right now (it answered with error 522). Try again "
            "in a few minutes."
        )


def test_a_buggy_source_falls_back_to_the_web(ddg) -> None:
    with patch("remotesearch.router.source_dictionary", side_effect=KeyError("meanings")):
        answerer = Answerer()
    with fake_web({}):
        assert answerer.answer("define boil water").startswith("web: How Long to Boil Water")


def test_web_answers_from_search_snippets(ddg) -> None:
    with fake_web({}):
        assert Answerer().answer("how long to boil water to purify it") == (
            "web: How Long to Boil Water to Purify for Drinking (According to Science): "
            + load("ddgs_boil_water.json")[0]["body"]
        )


def test_web_prefers_the_instant_answer(ddg) -> None:
    with fake_web({DDG: load("ddg_instant_albedo.json")}):
        assert Answerer().answer("albedo").startswith("web: Albedo is the fraction of sunlight")


def test_web_says_when_the_search_is_down(ddg) -> None:
    ddg.errors = [DDGSException("No results found.")] * 3
    with fake_web({}):
        assert answer("how long to boil water") == (
            "DuckDuckGo isn't answering right now (it's limiting requests). Try again in a few "
            "minutes."
        )


def test_wikipedia_answers_while_search_is_down(ddg) -> None:
    ddg.errors = [DDGSException("No results found.")] * 3
    responses = {
        WIKI_SEARCH: load("wikipedia_search.json"),
        "https://en.wikipedia.org/api/": load("wikipedia_summary.json"),
    }
    with fake_web(responses):
        assert answer("photosynthesis").startswith("web: Photosynthesis is a system of")


def test_ai_writes_the_answer_from_every_result(ddg) -> None:
    with (
        fake_web({DDG: load("ddg_instant_albedo.json")}),
        patch.object(
            ai, "ask", return_value="It reflects about 30% of sunlight (Wikipedia)."
        ) as asked,
    ):
        assert Answerer(AI_CONFIG).answer("albedo of earth") == (
            "ai: It reflects about 30% of sunlight (Wikipedia)."
        )
    user = asked.call_args.args[2]
    assert "Reference: Albedo is the fraction" in user
    assert "[1] How Long to Boil Water" in user


def test_ai_failing_falls_back_to_the_plain_answer(ddg) -> None:
    with fake_web({}), patch.object(ai, "ask", return_value=None):
        assert Answerer(AI_CONFIG).answer("boil water").startswith("web: How Long to Boil Water")


def test_site_commands_search_one_site(ddg) -> None:
    ddg.rows = [
        {
            "title": "Best tent for the West Coast Trail? : r/WestCoastTrail",
            "body": "A light freestanding tent with a good fly.",
            "href": "https://www.reddit.com/r/WestCoastTrail/comments/abc/",
        }
    ]
    with fake_web({}):
        assert answer("reddit best tent for the west coast trail") == (
            "reddit: Best tent for the West Coast Trail? : r/WestCoastTrail: A light "
            "freestanding tent with a good fly."
        )
        answer("site mec.ca return policy")
        answer("youtube bear hang")
        answer("quora why is the sky blue")
    # A site with no matching result falls back to a plain web search, as other commands do.
    assert [q for q in ddg.queries if q.startswith("site:")] == [
        "site:reddit.com best tent for the west coast trail",
        "site:mec.ca return policy",
        "site:youtube.com bear hang",
        "site:quora.com why is the sky blue",
    ]
    assert ddg.queries[2] == "mec.ca return policy"


def test_site_command_needs_a_domain(ddg) -> None:
    with fake_web({}):
        answer("site nodomain boil water")
    assert ddg.queries == ["nodomain boil water"]


def test_site_command_uses_ai_when_it_can(ddg) -> None:
    with fake_web({}), patch.object(ai, "ask", return_value="Most say a freestanding tent."):
        reply = Answerer(AI_CONFIG).answer("reddit best tent for the west coast trail")
    assert reply == "reddit: Most say a freestanding tent."


def test_translate_uses_mymemory_or_the_model() -> None:
    url = "https://api.mymemory.translated.net/"
    with fake_web({url: load("mymemory_de.json")}) as calls:
        assert answer("translate where is the train station to german") == (
            "translate: German: Wo ist der Bahnhof"
        )
    assert calls[0][1]["q"] == "where is the train station"
    with fake_web({}) as calls, patch.object(ai, "translate", return_value="Wo ist der Bahnhof?"):
        assert Answerer(AI_CONFIG).answer("translate where is the station to german") == (
            "translate: German: Wo ist der Bahnhof?"
        )
    assert calls == []
    assert answer("translate hello") == (
        "translate: Text it as translate <words> to <language>, like 'translate where is the "
        "bus to french'."
    )


def test_drive_needs_to_between_the_places() -> None:
    assert answer("drive Vancouver") == (
        "drive: Text it as drive <place> to <place>, like 'drive Vancouver to Whistler'."
    )


@pytest.fixture
def state():
    with tempfile.TemporaryDirectory() as tmp:
        yield State(Path(tmp) / "state.json")


def long_answer(ddg) -> None:
    ddg.rows = [{"title": "Boiling water", "body": "boil " * 150, "href": "https://x.ca"}]


def test_more_sends_the_next_page(ddg, state: State) -> None:
    long_answer(ddg)
    responder = Responder(Answerer(), state, 300)
    with fake_web({}):
        first = responder.reply("+16045551234", "boil water")
    assert first.startswith("web: Boiling water: boil boil")
    assert first.endswith(MORE)
    pages = [first]
    while pages[-1].endswith(MORE):
        pages.append(responder.reply("+16045551234", "more"))
    assert len(pages) == 3
    assert all(len(p) <= 300 and sms_segments(p) <= 2 for p in pages)
    assert responder.reply("+16045551234", "More.") == NOTHING_MORE_TEXT
    # Each sender has their own pages.
    assert responder.reply("+16045559999", "more") == NOTHING_MORE_TEXT


def test_pages_survive_a_restart(ddg, state: State) -> None:
    long_answer(ddg)
    with fake_web({}):
        Responder(Answerer(), state, 300).reply("me@example.com", "boil water")
    reloaded = Responder(Answerer(), State(state.path), 300)
    assert reloaded.reply("me@example.com", "more").startswith("boil boil")
    with fake_web({DICTIONARY: ALBEDO}):
        reloaded.reply("me@example.com", "define albedo")
    assert reloaded.reply("me@example.com", "more") == NOTHING_MORE_TEXT


def test_email_gets_the_whole_answer_unfolded(ddg, state: State) -> None:
    ddg.rows = [{"title": "Forêt", "body": "word " * 200, "href": "https://x.ca"}]
    with fake_web({}):
        reply = Responder(Answerer(), state, 300).reply("me@example.com", "foret", "email")
    assert reply.startswith("web: Forêt: word word")
    assert len(reply) > 1000
    assert state.data.get("pages", {}) == {}


def test_every_command_reply_fits_two_segments() -> None:
    """Each recorded source's reply, sent the way an SMS goes out, at the default size."""
    responses = {
        OPEN_METEO_GEOCODE: load("open_meteo_geocode_tofino.json"),
        OPEN_METEO: lambda url, params: load(
            "open_meteo_daily.json"
            if params.get("forecast_days") == 3
            else "open_meteo_current.json"
        ),
        "https://api.weather.gc.ca/": load("ec_alerts.json"),
        "https://api-iwls.dfo-mpo.gc.ca/api/v1/stations/": load("iwls_tofino_hilo.json"),
        "https://api-iwls.dfo-mpo.gc.ca/api/v1/stations": load("iwls_stations.json"),
        "https://api.avalanche.ca/": load("made_avalanche_winter.json"),
        "https://api.open511.gov.bc.ca/": load("open511_highway_99.json"),
        "https://api.frankfurter.dev/": load("frankfurter_usd_cad.json"),
        "https://news.google.com/rss": fixture_text("google_news_wildfire.xml"),
        "https://nominatim.openstreetmap.org/": load("nominatim_tim_hortons_hope.json"),
        "https://api.stackexchange.com/2.3/search": load("stackexchange_search.json"),
        "https://api.stackexchange.com/2.3/questions": load("stackexchange_answers.json"),
    }
    texts = [
        "weather Tofino",
        "forecast Tofino",
        "tide Tofino",
        "avy Tofino",
        "road hwy 99",
        "convert 50 usd to cad",
        "news wildfire",
        "hours Tim Hortons Hope",
        "so python yield",
        "calc 2^64",
        "help",
        "help weather",
    ]
    with fake_web(responses):
        for text in texts:
            reply = answer(text)
            assert len(reply) <= 300, text
            assert sms_segments(reply) <= 2, text
            # A calc reply keeps its ^, which GSM-7 sends as two characters.
            assert all(c in GSM7_BASIC or c in GSM7_EXTENSION for c in reply), text
