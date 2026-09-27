from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from conftest import OPEN_METEO, OPEN_METEO_GEOCODE, fake_web, load

from remotesearch.net import SourceError
from remotesearch.outdoors import (
    ec_alerts,
    source_avalanche,
    source_drive,
    source_forecast,
    source_roads,
    source_sun,
    source_tides,
    source_weather,
)
from remotesearch.places import geocode, place_label

EC = "https://api.weather.gc.ca/"
IWLS_STATIONS = "https://api-iwls.dfo-mpo.gc.ca/api/v1/stations"
IWLS_DATA = "https://api-iwls.dfo-mpo.gc.ca/api/v1/stations/"
AVALANCHE = "https://api.avalanche.ca/"
OPEN511 = "https://api.open511.gov.bc.ca/"
OSRM = "https://router.project-osrm.org/"
# Before the recorded alerts expire and before the recorded tides, on a Sunday in Tofino.
NOW = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)

TOFINO = load("open_meteo_geocode_tofino.json")


def place(name: str, lat: float, lon: float, admin1: str, country: str = "CA") -> dict[str, Any]:
    hit = {"name": name, "latitude": lat, "longitude": lon, "admin1": admin1}
    return {"results": [hit | {"country_code": country, "timezone": "America/Vancouver"}]}


@pytest.fixture(autouse=True)
def _clock() -> Any:
    with patch("remotesearch.outdoors._now", return_value=NOW):
        yield


def test_place_label() -> None:
    hit = {"name": "Tofino", "admin1": "British Columbia", "country_code": "CA"}
    assert place_label(hit) == "Tofino, British Columbia, CA"
    assert place_label({"name": "Atlantis"}) == "Atlantis"


def test_geocode_is_asked_once_per_name() -> None:
    with fake_web({OPEN_METEO_GEOCODE: TOFINO}) as calls:
        geocode("Tofino")
        geocode("Tofino")
    assert len(calls) == 1


def test_weather_reads_the_recorded_conditions_and_alerts() -> None:
    responses = {
        OPEN_METEO_GEOCODE: TOFINO,
        OPEN_METEO: load("open_meteo_current.json"),
        EC: load("ec_alerts.json"),
    }
    with fake_web(responses) as calls:
        assert source_weather("in Tofino") == (
            "Tofino, British Columbia, CA: clear, 12C (feels 11C), wind 1km/h, humidity 78%. "
            "ALERT: frost advisory"
        )
    assert calls[0][1]["name"] == "Tofino"  # "in" is dropped before geocoding
    assert calls[2][1]["bbox"] == "-125.9174,49.1431,-125.8974,49.1631"


def test_weather_without_alerts_or_outside_canada() -> None:
    current = load("open_meteo_current.json")
    with fake_web({OPEN_METEO_GEOCODE: TOFINO, OPEN_METEO: current, EC: {"features": []}}):
        assert source_weather("Tofino") == (
            "Tofino, British Columbia, CA: clear, 12C (feels 11C), wind 1km/h, humidity 78%"
        )
    seattle = place("Seattle", 47.6, -122.3, "Washington", "US")
    with fake_web({OPEN_METEO_GEOCODE: seattle, OPEN_METEO: current}) as calls:
        assert source_weather("Seattle") == (
            "Seattle, Washington, US: clear, 12C (feels 11C), wind 1km/h, humidity 78%"
        )
    assert not any(url.startswith(EC) for url, _ in calls)


def test_weather_still_answers_when_the_alerts_are_down() -> None:
    responses = {
        OPEN_METEO_GEOCODE: TOFINO,
        OPEN_METEO: load("open_meteo_current.json"),
        EC: SourceError("Environment Canada", "it timed out"),
    }
    with fake_web(responses):
        assert source_weather("Tofino") == (
            "Tofino, British Columbia, CA: clear, 12C (feels 11C), wind 1km/h, humidity 78%. "
            "Couldn't check for weather alerts"
        )


def test_weather_for_an_unknown_place() -> None:
    with fake_web({OPEN_METEO_GEOCODE: {"generationtime_ms": 0.1}}):
        assert source_weather("Zzqxville") is None


def test_ec_alerts_only_in_force() -> None:
    alerts = load("ec_alerts.json")
    tofino = TOFINO["results"][0]
    with fake_web({EC: alerts}):
        assert ec_alerts(tofino) == ["frost advisory"]
    with (
        fake_web({EC: alerts}),
        patch("remotesearch.outdoors._now", return_value=datetime(2026, 9, 29, tzinfo=UTC)),
    ):
        assert ec_alerts(tofino) == []


def test_forecast_reads_three_recorded_days() -> None:
    responses = {
        OPEN_METEO_GEOCODE: TOFINO,
        OPEN_METEO: load("open_meteo_daily.json"),
        EC: {"features": []},
    }
    with fake_web(responses) as calls:
        assert source_forecast("Tofino") == (
            "Tofino, British Columbia, CA: Sun overcast 15/6C, 2% 0mm, wind 15km/h. "
            "Mon heavy rain 14/10C, 100% 41mm, wind 27km/h. "
            "Tue light rain 15/10C, 96% 10mm, wind 23km/h"
        )
    assert calls[1][1]["forecast_days"] == 3


def test_forecast_stops_at_missing_days() -> None:
    daily = load("open_meteo_daily.json")
    daily["daily"]["temperature_2m_max"][1] = None
    daily["daily"]["precipitation_probability_max"][0] = 0
    with fake_web({OPEN_METEO_GEOCODE: TOFINO, OPEN_METEO: daily, EC: {"features": []}}):
        assert source_forecast("Tofino") == (
            "Tofino, British Columbia, CA: Sun overcast 15/6C, wind 15km/h"
        )
    with fake_web({OPEN_METEO_GEOCODE: TOFINO, OPEN_METEO: {"daily": {}}}):
        assert source_forecast("Tofino") is None


def test_sun_names_the_zone_and_skips_polar_days() -> None:
    responses = {OPEN_METEO_GEOCODE: TOFINO, OPEN_METEO: load("open_meteo_sun.json")}
    with fake_web(responses):
        assert (
            source_sun("Tofino")
            == "Tofino, British Columbia, CA: sunrise 07:17, sunset 19:10 (PDT)"
        )
    polar = load("open_meteo_sun.json")
    polar["daily"] = {"sunrise": [None], "sunset": [None]}
    with fake_web({OPEN_METEO_GEOCODE: TOFINO, OPEN_METEO: polar}):
        assert source_sun("Tofino") is None


def test_tides_at_the_nearest_station_in_local_time() -> None:
    responses = {
        OPEN_METEO_GEOCODE: TOFINO,
        IWLS_DATA: load("iwls_tofino_hilo.json"),
        IWLS_STATIONS: load("iwls_stations.json"),
    }
    with fake_web(responses) as calls:
        assert source_tides("Tofino") == (
            "Tofino: high 13:42 3.6m, low 20:07 0.6m, high Mon 02:17 3.3m, low Mon 08:06 1.1m (PDT)"
        )
    assert calls[2] == (
        "https://api-iwls.dfo-mpo.gc.ca/api/v1/stations/5cebf1e23d0f4a073c4bc07c/data",
        {
            "time-series-code": "wlp-hilo",
            "from": "2026-09-27T17:00:00Z",
            "to": "2026-09-28T18:00:00Z",
        },
    )


def test_tides_name_a_distant_station() -> None:
    bamfield = place("Bamfield", 48.83, -125.14, "British Columbia")
    responses = {
        OPEN_METEO_GEOCODE: bamfield,
        IWLS_DATA: load("iwls_tofino_hilo.json"),
        IWLS_STATIONS: load("iwls_stations.json"),
    }
    with fake_web(responses):
        reply = source_tides("Bamfield")
    assert reply is not None
    assert reply.startswith("Ucluelet (33km away): high 13:42 3.6m")


def test_tides_inland_and_with_too_little_data() -> None:
    calgary = place("Calgary", 51.05, -114.07, "Alberta")
    with fake_web({OPEN_METEO_GEOCODE: calgary, IWLS_STATIONS: load("iwls_stations.json")}):
        assert source_tides("Calgary") == (
            "There's no Canadian tide station within 100km of Calgary, Alberta, CA."
        )
    one = load("iwls_tofino_hilo.json")[:1]
    responses = {
        OPEN_METEO_GEOCODE: TOFINO,
        IWLS_DATA: one,
        IWLS_STATIONS: load("iwls_stations.json"),
    }
    with fake_web(responses):
        assert source_tides("Tofino") is None


def test_tide_stations_are_fetched_once_per_run() -> None:
    responses = {
        OPEN_METEO_GEOCODE: TOFINO,
        IWLS_DATA: load("iwls_tofino_hilo.json"),
        IWLS_STATIONS: load("iwls_stations.json"),
    }
    with fake_web(responses) as calls:
        source_tides("Tofino")
        source_tides("Tofino")
    urls = [url for url, _ in calls]
    assert urls.count(IWLS_STATIONS) == 1
    assert sum(url.endswith("/data") for url in urls) == 2  # tide times are never cached


def test_avalanche_in_season() -> None:
    with fake_web({OPEN_METEO_GEOCODE: TOFINO, AVALANCHE: load("made_avalanche_winter.json")}):
        assert source_avalanche("Tofino") == (
            "Avalanche Canada, Sea To Sky, Friday: alpine 3 Considerable, treeline 2 Moderate, "
            "below treeline 1 Low. Problems: Wind slab, Persistent slab. Wind slabs are building "
            "on lee slopes near ridgetops. Be careful around steep, convex terrain."
        )


def test_avalanche_out_of_season_and_out_of_range() -> None:
    with fake_web({OPEN_METEO_GEOCODE: TOFINO, AVALANCHE: load("avalanche_offseason.json")}):
        assert source_avalanche("Tofino") == (
            "Avalanche Canada: Regular avalanche forecasts have ended for the season and will "
            "resume in November. Avalanche danger may still exist in some high elevation areas."
        )
    with fake_web({OPEN_METEO_GEOCODE: TOFINO, AVALANCHE: load("avalanche_outside.json")}):
        assert source_avalanche("Tofino") == (
            "Avalanche Canada has no forecast covering Tofino, British Columbia, CA."
        )


def test_roads_on_a_numbered_highway() -> None:
    with fake_web({OPEN511: load("open511_highway_99.json")}) as calls:
        reply = source_roads("hwy 99")
    assert reply == (
        "DriveBC, Highway 99, 3 events: Utility work from Highway 99, In Vancouver and W 70th "
        "Ave to In Vancouver and W 71 Ave. Starting Mon Aug 24 until Sat Oct 31. From 9:30 AM "
        "to 3:00 PM PDT, Monday to Thursday. Right lane closed. / Mowing from Highway 99, In "
        "Whistler and At Alpha Lake Road to In Squamish and 453m North of Squamish Nation Ped "
        "Overpass. Starting Mon Sep 28 until Fri Oct 2. From 7:30 AM to 3:30 PM PDT, weekdays. "
        "Shoulder closed. Slow moving vehicle operations. No work scheduled on Sept 30th. / "
        "Ditch maintenance from Highway 99, 413m Southeast of 90 Kilometre Post to At Brunswick "
        "North Bridge. Starting Thu Oct 1 until Thu Oct 1. Right lane blocked."
    )
    assert calls[0][1] == {
        "format": "json",
        "status": "ACTIVE",
        "limit": 100,
        "road_name": "Highway 99",
    }


@pytest.mark.parametrize(
    ("text", "road"),
    [
        ("99", "Highway 99"),
        ("Highway 1", "Highway 1"),
        ("coquihalla", "Highway 5"),
        ("Sea to Sky", "Highway 99"),
        ("hwy 97c", "Highway 97C"),
    ],
)
def test_roads_by_number_or_by_name(text: str, road: str) -> None:
    with fake_web({OPEN511: {"events": []}}) as calls:
        reply = source_roads(text)
    assert calls[0][1]["road_name"] == road
    assert reply == f"No DriveBC events on {road} right now."


def test_roads_near_a_town_worst_first() -> None:
    squamish = place("Squamish", 49.7, -123.16, "British Columbia")
    events = load("open511_highway_99.json")
    events["events"].append(
        {"event_type": "INCIDENT", "severity": "MAJOR", "description": "Crash. Road closed."}
    )
    with fake_web({OPEN_METEO_GEOCODE: squamish, OPEN511: events}) as calls:
        reply = source_roads("Squamish")
    assert reply is not None
    assert reply.startswith("DriveBC, near Squamish, 4 events: Crash. Road closed. / Utility work")
    assert calls[1][1]["bbox"] == "-123.4600,49.5000,-122.8600,49.9000"


def test_roads_outside_bc() -> None:
    banff = place("Banff", 51.18, -115.57, "Alberta")
    with fake_web({OPEN_METEO_GEOCODE: banff}) as calls:
        assert (
            source_roads("Banff")
            == "DriveBC only covers BC roads, and Banff, Alberta, CA isn't in BC."
        )
    assert len(calls) == 1


def test_drive_reads_the_recorded_route() -> None:
    vancouver = place("Vancouver", 49.2827, -123.1207, "British Columbia")
    whistler = place("Whistler", 50.1163, -122.9574, "British Columbia")
    responses = {
        OPEN_METEO_GEOCODE: lambda url, params: (
            vancouver if params["name"] == "Vancouver" else whistler
        ),
        OSRM: load("osrm_vancouver_whistler.json"),
    }
    with fake_web(responses) as calls:
        assert source_drive("from Vancouver to Whistler") == (
            "Vancouver, British Columbia, CA to Whistler, British Columbia, CA: 122km, about 1 h "
            "59 min driving (OSRM, no traffic or closures)"
        )
    assert calls[2][0] == (
        "https://router.project-osrm.org/route/v1/driving/-123.1207,49.2827;-122.9574,50.1163"
    )


def test_drive_refuses_a_route_snapped_across_an_ocean() -> None:
    vancouver = place("Vancouver", 49.2827, -123.1207, "British Columbia")
    london = place("London", 51.5072, -0.1276, "England", "GB")
    responses = {
        OPEN_METEO_GEOCODE: lambda url, params: (
            vancouver if params["name"] == "Vancouver" else london
        ),
        OSRM: load("osrm_vancouver_london.json"),
    }
    with fake_web(responses):
        assert source_drive("Vancouver to London") == (
            "There's no road route from Vancouver, British Columbia, CA to London, England, GB."
        )


def test_drive_needs_two_places() -> None:
    with fake_web({}) as calls:
        assert source_drive("Vancouver") is None
        assert source_drive("Vancouver to Zzqx") is None
    assert len(calls) == 2  # it never asked OSRM
