from conftest import DDG, WIKI_SEARCH, WIKI_SUMMARY, fake_web, load

from remotesearch.lookup import (
    WIKTIONARY,
    content_words,
    looks_relevant,
    source_dictionary,
    source_duckduckgo,
    source_stackoverflow,
    source_wikipedia,
)


def test_content_words_drops_stopwords() -> None:
    assert content_words("why is the sky blue") == {"sky", "blue"}
    assert content_words("How do I treat a BLISTER") == {"treat", "blister"}


def test_looks_relevant_rejects_the_real_misfires() -> None:
    """These are the actual top Wikipedia hits for these texts.

    Wikipedia search returns something for any query built from real words, so without
    this check a first-aid question got a travel memoir and a road question got a
    highway in another province.
    """
    assert looks_relevant("why is the sky blue", "Diffuse sky radiation") is True
    assert looks_relevant("how long to boil water to purify it", "Purified water") is True
    assert looks_relevant("photosynthesis", "Photosynthesis") is True

    assert looks_relevant("how do I treat a blister on a hike", "The Salt Path") is False
    assert looks_relevant("what time is sunset in Tofino", "British Columbia") is False
    assert looks_relevant("best hiking boots", "Vibram S.p.A.") is False


def test_looks_relevant_stem_matching() -> None:
    assert looks_relevant("purify water", "Purified water") is True  # shared 5-letter prefix
    assert looks_relevant("open the gate", "Opera house") is False  # 3 letters isn't enough
    assert looks_relevant("montreal forets", "Forêts de Montréal") is True  # accents drop out


def test_looks_relevant_matches_short_word_queries() -> None:
    assert looks_relevant("AC/DC", "AC/DC") is True
    assert looks_relevant("UV", "Ultraviolet") is False


def test_wikipedia_reads_the_recorded_summary() -> None:
    responses = {
        WIKI_SEARCH: load("wikipedia_search.json"),
        WIKI_SUMMARY: load("wikipedia_summary.json"),
    }
    with fake_web(responses) as calls:
        result = source_wikipedia("photosynthesis")
    assert result is not None
    assert result.startswith("Photosynthesis is a system of biological processes by which")
    assert calls[1][0] == WIKI_SUMMARY + "Photosynthesis"


def test_wikipedia_escapes_a_slash_in_the_title() -> None:
    responses = {
        WIKI_SEARCH: {"query": {"search": [{"title": "AC/DC"}]}},
        WIKI_SUMMARY: {"type": "standard", "extract": "AC/DC are an Australian rock band."},
    }
    with fake_web(responses) as calls:
        assert source_wikipedia("AC/DC") == "AC/DC are an Australian rock band."
    assert calls[1][0] == WIKI_SUMMARY + "AC%2FDC"


def test_wikipedia_prefixes_the_article_title() -> None:
    responses = {
        WIKI_SEARCH: {"query": {"search": [{"title": "Purified water"}]}},
        WIKI_SUMMARY: {"type": "standard", "extract": "Water that has been filtered.[1]"},
    }
    with fake_web(responses) as calls:
        result = source_wikipedia("purify water")
    assert result == "Purified water: Water that has been filtered."
    assert calls[1][0] == WIKI_SUMMARY + "Purified_water"


def test_wikipedia_skips_disambiguation_pages_and_empty_searches() -> None:
    responses = {
        WIKI_SEARCH: {"query": {"search": [{"title": "Mercury"}]}},
        WIKI_SUMMARY: {"type": "disambiguation", "extract": "Mercury may refer to:"},
    }
    with fake_web(responses):
        assert source_wikipedia("mercury") is None
    with fake_web({WIKI_SEARCH: {"query": {"search": []}}}):
        assert source_wikipedia("zzqx") is None


def test_duckduckgo_reads_the_recorded_abstract() -> None:
    with fake_web({DDG: load("ddg_instant_albedo.json")}):
        result = source_duckduckgo("albedo")
    assert result is not None
    assert result.startswith("Albedo is the fraction of sunlight that is diffusely reflected")


def test_duckduckgo_related_topic_needs_a_matching_title() -> None:
    topics = {
        "AbstractText": "",
        "RelatedTopics": [
            {"FirstURL": "https://duckduckgo.com/Hiking_boot", "Text": "Hiking boot, for blisters"},
            {"FirstURL": "https://duckduckgo.com/Blister", "Text": "Blister A pocket of fluid."},
        ],
    }
    with fake_web({DDG: topics}):
        assert source_duckduckgo("treat a blister") == "Blister A pocket of fluid."
        assert source_duckduckgo("best tent stakes") is None


def test_duckduckgo_prefers_a_direct_answer() -> None:
    with fake_web({DDG: {"Answer": "", "AbstractText": " Albedo is reflectance. "}}):
        assert source_duckduckgo("albedo") == "Albedo is reflectance."
    with fake_web({DDG: ["not", "a", "dict"]}):
        assert source_duckduckgo("albedo") is None


def test_dictionary_reads_the_recorded_entry() -> None:
    with fake_web({WIKTIONARY: load("wiktionary_portage.json")}) as calls:
        assert source_dictionary("portage") == (
            "(noun) An act of carrying, especially the carrying of a boat overland between two "
            "waterways. (verb) To carry a boat overland."
        )
    assert [url for url, _ in calls] == [WIKTIONARY + "portage"]


def test_dictionary_takes_two_senses_and_skips_empty_ones() -> None:
    entry = {
        "en": [
            {
                "partOfSpeech": "Noun",
                "definitions": [{"definition": "<span></span>"}, {"definition": "Reflectivity."}],
            },
            {"partOfSpeech": "Verb", "definitions": [{"definition": "To reflect."}]},
            {"partOfSpeech": "Adjective", "definitions": [{"definition": "Unused."}]},
        ]
    }
    with fake_web({WIKTIONARY: entry}) as calls:
        assert source_dictionary("half life") == "(noun) Reflectivity. (verb) To reflect."
    assert [url for url, _ in calls] == [WIKTIONARY + "half%20life"]


def test_dictionary_tries_a_capitalized_word_in_lower_case() -> None:
    german = {"de": [{"partOfSpeech": "Noun", "definitions": [{"definition": "albedo"}]}]}
    english = {"en": [{"partOfSpeech": "Noun", "definitions": [{"definition": "Reflectivity."}]}]}

    def respond(url: str, _params: dict) -> dict:
        return german if url.endswith("Albedo") else english

    with fake_web({WIKTIONARY: respond}) as calls:
        assert source_dictionary("Albedo") == "(noun) Reflectivity."
    assert [url for url, _ in calls] == [WIKTIONARY + "Albedo", WIKTIONARY + "albedo"]
    # Wiktionary answers a word it doesn't have with a 404, which comes through as None.
    with fake_web({}) as calls:
        assert source_dictionary("zzqx") is None
    assert len(calls) == 1


def test_stackoverflow_reads_the_recorded_answer() -> None:
    responses = {
        "https://api.stackexchange.com/2.3/search/advanced": load("stackexchange_search.json"),
        "https://api.stackexchange.com/2.3/questions/231767/answers": load(
            "stackexchange_answers.json"
        ),
    }
    with fake_web(responses):
        result = source_stackoverflow("python yield")
    assert result is not None
    assert result.startswith(
        'What does the "yield" keyword do in Python? To understand what yield does, you must '
        "understand what generators are."
    )


def test_stackoverflow_question_with_no_answers() -> None:
    responses = {
        "https://api.stackexchange.com/2.3/search/advanced": {
            "items": [{"title": "Why &amp; how?", "question_id": 7}]
        },
        "https://api.stackexchange.com/2.3/questions/7/answers": {"items": []},
    }
    with fake_web(responses):
        assert source_stackoverflow("why") == "Why & how? (no answers yet)"
