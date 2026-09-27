import re
from unittest.mock import patch

import pytest
from conftest import OPEN_METEO_GEOCODE, fake_web, fixture_text, load

from remotesearch.net import SourceError
from remotesearch.news import game_line, news_edition, source_news, source_scores
from remotesearch.places import source_business, source_time
from remotesearch.tools import (
    CalcError,
    calculate,
    mymemory,
    source_calc,
    source_convert,
    translation_request,
)

ESPN = "https://site.api.espn.com/"
NOMINATIM = "https://nominatim.openstreetmap.org/"
NEWS = "https://news.google.com/rss"
CANADA = {"hl": "en-CA", "gl": "CA", "ceid": "CA:en"}


@pytest.mark.parametrize(
    ("expression", "value"),
    [
        ("2+2*3", 8),
        ("(3+4)^2", 49),
        ("15% of 80", 12),
        ("3 x 4", 12),
        ("1,000*3", 3000),
        ("10/3", 10 / 3),
        ("2**0.5", 2**0.5),
        ("-5^2", -25),
        ("sqrt(16) + abs(-2)", 6),
        ("sin(30)", 0.5),
        ("log(1000)", 3),
        ("round(2.6)", 3),
        ("2 * pi", 6.283185307179586),
    ],
)
def test_calculate(expression: str, value: float) -> None:
    assert calculate(expression) == pytest.approx(value)


@pytest.mark.parametrize(
    ("expression", "why"),
    [
        ("9**9**9", "that power is too big to work out"),
        ("99**999", "the answer is out of range"),
        ("1/0", "it can't divide by zero"),
        ("sqrt(-1)", "the answer is out of range"),
        ("(1+2", "that isn't an expression it can read"),
        ("1" * 101, "that's too long to work out"),
        ("__import__('os').system('ls')", "it only does numbers"),
        ("().__class__", "it only does numbers"),
        ("x = 1", "that isn't an expression it can read"),
        ("[1, 2]", "it only does numbers"),
        ("true + 1", "it only does numbers"),
        ("sqrt(4, 2)", "it only does numbers"),
    ],
)
def test_calculate_refuses_anything_but_arithmetic(expression: str, why: str) -> None:
    with pytest.raises(CalcError, match=re.escape(why)):
        calculate(expression)


def test_calc_reply() -> None:
    assert source_calc("15% of 80") == "15% of 80 = 12"
    assert source_calc("10/3") == "10/3 = 3.333333333"
    assert source_calc("2^20") == "2^20 = 1,048,576"
    assert source_calc("1/0") == "Couldn't work out '1/0', as it can't divide by zero."


@pytest.mark.parametrize(
    ("text", "reply"),
    [
        ("10 km to mi", "10 km = 6.2137 mi"),
        ("5 feet in metres", "5 feet = 1.524 metres"),
        ("8 fl oz to ml", "8 fl oz = 236.5882 ml"),
        ("100 kph to mph", "100 kph = 62.1371 mph"),
        ("10 in to cm", "10 in = 25.4 cm"),
        ("3 Kg to lbs", "3 Kg = 6.6139 lbs"),
        ("1,500 m to km", "1,500 m = 1.5 km"),
        ("70 f to c", "70F = 21.11C"),
        ("-40 c to f", "-40C = -40F"),
        ("300 kelvin to degrees celsius", "300K = 26.85C"),
        ("10 km to kg", "Can't convert length to weight."),
    ],
)
def test_convert_units(text: str, reply: str) -> None:
    with fake_web({}) as calls:
        assert source_convert(text) == reply
    assert calls == []


def test_convert_currency_reads_the_recorded_rate() -> None:
    with fake_web({"https://api.frankfurter.dev/": load("frankfurter_usd_cad.json")}) as calls:
        assert source_convert("50 usd to cad") == "50 USD = 70.72 CAD (ECB rate for 2026-09-25)"
    assert calls == [
        (
            "https://api.frankfurter.dev/v1/latest",
            {"base": "USD", "symbols": "CAD", "amount": 50.0},
        )
    ]


def test_convert_an_unknown_currency_or_something_else_entirely() -> None:
    # Frankfurter answers an unknown code with a 404, which comes through as None.
    with fake_web({}):
        assert source_convert("50 usd to xyz") == (
            "Frankfurter has no rate from USD to XYZ. It covers about 30 currencies from the "
            "European Central Bank."
        )
        assert source_convert("pdf to word") is None
        assert source_convert("5 parsecs to furlongs") is None


def test_translation_request() -> None:
    assert translation_request("where is the bus to french") == ("where is the bus", "French", "fr")
    assert translation_request("go to the store into Spanish.") == (
        "go to the store",
        "Spanish",
        "es",
    )
    assert translation_request("hello to klingon") is None
    assert translation_request("hello") is None


def test_mymemory_reads_the_recorded_translation_and_its_errors() -> None:
    url = "https://api.mymemory.translated.net/"
    with fake_web({url: load("mymemory_de.json")}) as calls:
        assert mymemory("Where is the train station", "de") == "Wo ist der Bahnhof"
    assert calls[0][1] == {"q": "Where is the train station", "langpair": "autodetect|de"}
    spent = load("mymemory_de.json") | {"quotaFinished": True}
    with fake_web({url: spent}), pytest.raises(SourceError, match="out of free translations"):
        mymemory("hello", "de")
    bad = {"responseStatus": 403, "responseDetails": "'XX' IS AN INVALID TARGET LANGUAGE"}
    with fake_web({url: bad}), pytest.raises(SourceError, match="INVALID TARGET LANGUAGE"):
        mymemory("hello", "xx")


def test_time_in_a_place() -> None:
    with fake_web({OPEN_METEO_GEOCODE: load("open_meteo_geocode_tofino.json")}):
        reply = source_time("in Tofino")
    assert reply is not None
    assert re.fullmatch(
        r"Tofino, British Columbia, CA: \d\d:\d\d \w{3} \w{3} \d{1,2} \(P[DS]T\)", reply
    ), reply
    with fake_web({}):
        assert source_time("Zzqx") is None


def test_business_reads_the_recorded_place() -> None:
    with fake_web({NOMINATIM: load("nominatim_tim_hortons_hope.json")}) as calls:
        assert source_business("Tim Hortons in Hope BC") == (
            "Tim Hortons, 250 Old Hope-Princeton Way, Hope. Open Mo-Su 05:00-22:00. +1-604-860-0601"
        )
        source_business("Tim Hortons in Hope BC")
    assert calls == [
        (
            "https://nominatim.openstreetmap.org/search",
            {
                "q": "Tim Hortons, Hope BC",
                "format": "jsonv2",
                "extratags": 1,
                "addressdetails": 1,
                "limit": 3,
            },
        )
    ]  # asked once, as Nominatim's policy wants results kept


def test_business_skips_the_wrong_place() -> None:
    wrong = [{"name": "Hope Station House", "extratags": {"opening_hours": "24/7"}, "address": {}}]
    with fake_web({NOMINATIM: wrong}):
        assert source_business("Tim Hortons") is None
    bare = [{"name": "Tim Hortons", "address": {"road": "Main St", "town": "Hope"}}]
    with fake_web({NOMINATIM: bare}):
        assert source_business("Tim Hortons Hope") == "Tim Hortons, Main St, Hope. No hours listed"


def test_business_waits_a_second_between_nominatim_calls() -> None:
    with patch("remotesearch.net.session") as session, patch("time.sleep") as sleep:
        session.return_value.get.return_value.status_code = 200
        session.return_value.get.return_value.json.return_value = []
        source_business("first place")
        source_business("second place")
    waits = [c.args[0] for c in sleep.call_args_list]
    assert len(waits) == 1
    assert 0.9 < waits[0] <= 1.0


def test_news_reads_the_recorded_feed() -> None:
    with fake_web({NEWS: fixture_text("google_news_wildfire.xml")}) as calls:
        reply = source_news("wildfire BC", CANADA)
    assert reply == (
        "1) \N{LEFT SINGLE QUOTATION MARK}God cleaned the slate\N{RIGHT SINGLE QUOTATION MARK}: "
        "Ashes pave the road to recovery after B.C. wildfires - Global News. 2) LETTER: BC "
        "Wildfire Service leadership questioned - Kelowna Capital News. 3) Growing wildfire "
        "burning out of control on Vancouver Island - CTV News."
    )
    assert calls == [("https://news.google.com/rss/search", {"q": "wildfire BC", **CANADA})]
    with fake_web({NEWS: fixture_text("google_news_wildfire.xml")}) as calls:
        source_news("", CANADA)
    assert calls[0] == ("https://news.google.com/rss", CANADA)


def test_news_with_a_broken_or_empty_feed() -> None:
    with fake_web({NEWS: "<rss><channel><item><title>cut off"}), pytest.raises(SourceError):
        source_news("x", CANADA)
    with fake_web({NEWS: "<rss><channel></channel></rss>"}):
        assert source_news("x", CANADA) is None


def test_news_edition_reads_news_region() -> None:
    assert news_edition("CA") == CANADA
    assert news_edition(" gb ") == {"hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    assert news_edition("CA:fr") == {"hl": "fr-CA", "gl": "CA", "ceid": "CA:fr"}
    for bad in ("Canada", "en-CA", "C:fr", "CA:french"):
        with pytest.raises(SystemExit, match=f"NEWS_REGION is {bad!r}"):
            news_edition(bad)


def boards(url: str, params: dict) -> dict:
    """Today's NHL board and yesterday's MLB one, recorded, with nothing on the rest."""
    if "hockey/nhl" in url and not params:
        return load("espn_nhl_scoreboard.json")
    if "baseball/mlb" in url and params.get("dates"):
        return load("espn_mlb_yesterday.json")
    return {"events": []}


def test_scores_by_team_name() -> None:
    with fake_web({ESPN: boards}) as calls:
        assert source_scores("Canucks") == "VAN @ EDM, 9/29 - 10:00 PM EDT"
        assert source_scores("blue jays") == "CIN 5 @ TOR 1, Final"
    # Seven leagues, today and yesterday, per question.
    assert len(calls) == 28
    assert calls[0] == ("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard", {})
    assert re.fullmatch(r"\d{8}", calls[1][1]["dates"])


def test_scores_put_finals_before_games_to_come() -> None:
    with fake_web({ESPN: boards}):
        assert source_scores("tor") == "CIN 5 @ TOR 1, Final / MTL @ TOR, 9/29 - 7:00 PM EDT"
        assert source_scores("to") is None  # too short to match inside a name


def test_scores_when_espn_is_down() -> None:
    with fake_web({ESPN: SourceError("ESPN", "it timed out")}), pytest.raises(SourceError):
        source_scores("canucks")
    with fake_web({ESPN: {"events": []}}):
        assert source_scores("canucks") is None


def test_game_line_while_a_game_is_on() -> None:
    event = load("espn_nhl_scoreboard.json")["events"][1]
    event["status"]["type"] |= {"state": "in", "shortDetail": "2nd 10:32"}
    sides = event["competitions"][0]["competitors"]
    sides[0]["score"], sides[1]["score"] = "1", "2"
    assert game_line(event) == "VAN 2 @ EDM 1, 2nd 10:32"


def test_business_skips_the_town_itself() -> None:
    found = [
        {"name": "Squamish", "category": "boundary", "address": {}},
        {
            "name": "Save-On-Foods",
            "category": "shop",
            "extratags": {"opening_hours": "Mo-Su 09:00-21:00"},
            "address": {"road": "Third Avenue", "town": "Squamish"},
        },
    ]
    with fake_web({NOMINATIM: found}):
        assert source_business("Save-On-Foods Squamish") == (
            "Save-On-Foods, Third Avenue, Squamish. Open Mo-Su 09:00-21:00"
        )
