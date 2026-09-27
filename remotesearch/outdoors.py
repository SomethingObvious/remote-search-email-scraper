"""Weather, alerts, sun, tides, avalanche danger, BC road events and driving time."""

import math
import re
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

from . import net
from .net import SourceError
from .places import geocode, place_label, place_words
from .text import html_to_text

# Open-Meteo returns a WMO weather code, and this names it.
WMO_CODES = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def _place(text: str) -> dict[str, Any] | None:
    return geocode(place_words(text))


def _forecast(hit: dict[str, Any], days: int = 1, **fields: str) -> dict[str, Any] | None:
    data = net.get_json(
        "https://api.open-meteo.com/v1/forecast",
        "Open-Meteo",
        params={
            "latitude": hit["latitude"],
            "longitude": hit["longitude"],
            "timezone": "auto",
            "forecast_days": days,
            **fields,
        },
    )
    return data if isinstance(data, dict) else None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _now() -> datetime:
    return datetime.now(UTC)


def ec_alerts(hit: dict[str, Any]) -> list[str]:
    """Names of the Environment Canada alerts in force at a Canadian place."""
    if hit.get("country_code") != "CA":
        return []
    lat, lon = hit["latitude"], hit["longitude"]
    data = net.get_json(
        "https://api.weather.gc.ca/collections/weather-alerts/items",
        "Environment Canada",
        params={
            "f": "json",
            "bbox": f"{lon - 0.01:.4f},{lat - 0.01:.4f},{lon + 0.01:.4f},{lat + 0.01:.4f}",
            "skipGeometry": "true",
            "limit": 50,
        },
    )
    now = _now()
    names: list[str] = []
    for feature in (data or {}).get("features") or []:
        props = feature.get("properties") or {}
        expires = props.get("expiration_datetime")
        if props.get("status_en") == "ended" or (expires and _parse_time(expires) < now):
            continue
        name = props.get("alert_name_en")
        if name and name not in names:
            names.append(name)
    return names


def _with_alerts(hit: dict[str, Any], reply: str) -> str:
    try:
        alerts = ec_alerts(hit)
    except SourceError:
        return f"{reply}. Couldn't check for weather alerts"
    return f"{reply}. ALERT: {', '.join(alerts)}" if alerts else reply


def source_weather(place: str) -> str | None:
    """Current conditions from Open-Meteo, with C and km/h spelled out for SMS."""
    hit = _place(place)
    if not hit:
        return None
    data = _forecast(
        hit,
        current="temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
    )
    now = (data or {}).get("current")
    if not now:
        return None
    reply = (
        f"{place_label(hit)}: {WMO_CODES.get(now.get('weather_code'), 'unknown')}, "
        f"{now['temperature_2m']:.0f}C (feels {now['apparent_temperature']:.0f}C), "
        f"wind {now['wind_speed_10m']:.0f}km/h, humidity {now['relative_humidity_2m']:.0f}%"
    )
    return _with_alerts(hit, reply)


def source_forecast(place: str) -> str | None:
    """Three days of highs, lows, rain and wind, plus any Environment Canada alerts."""
    hit = _place(place)
    if not hit:
        return None
    data = _forecast(
        hit,
        days=3,
        daily="weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
    )
    daily = (data or {}).get("daily") or {}
    days = []
    for i, date in enumerate(daily.get("time") or []):
        try:
            high, low = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
            chance, rain = daily["precipitation_probability_max"][i], daily["precipitation_sum"][i]
            wind = daily["wind_speed_10m_max"][i]
            sky = WMO_CODES.get(daily["weather_code"][i], "unknown")
        except (KeyError, IndexError):
            break
        if high is None or low is None:
            break
        day = f"{datetime.fromisoformat(date):%a} {sky} {high:.0f}/{low:.0f}C"
        if chance:
            day += f", {chance:.0f}% {rain or 0:.0f}mm"
        days.append(f"{day}, wind {wind or 0:.0f}km/h")
    if not days:
        return None
    return _with_alerts(hit, f"{place_label(hit)}: {'. '.join(days)}")


def source_sun(place: str) -> str | None:
    """Today's sunrise and sunset in the place's own timezone."""
    hit = _place(place)
    if not hit:
        return None
    data = _forecast(hit, daily="sunrise,sunset") or {}
    daily = data.get("daily") or {}
    try:
        sunrise, sunset = daily["sunrise"][0], daily["sunset"][0]
    except (KeyError, IndexError):
        return None
    if not sunrise or not sunset:  # null above the Arctic Circle in midsummer and midwinter
        return None
    # Open-Meteo abbreviates Pacific time as GMT-7, so the name comes from zoneinfo.
    zone = f"{_now().astimezone(ZoneInfo(hit['timezone'])):%Z}" if hit.get("timezone") else ""
    zone = zone or data.get("timezone_abbreviation", "local")
    return f"{place_label(hit)}: sunrise {sunrise[11:16]}, sunset {sunset[11:16]} ({zone})"


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


IWLS = "https://api-iwls.dfo-mpo.gc.ca/api/v1"
TIDE_RANGE_KM = 100  # past this, the nearest station's tides say little about the place


# The station list only changes when a station opens or closes, so it's fetched once
# per run. The tide times themselves are fetched fresh every time.
@cache
def tide_stations() -> tuple[dict[str, Any], ...]:
    found = net.get_json(
        f"{IWLS}/stations", "the tide service", params={"time-series-code": "wlp-hilo"}
    )
    return tuple(s for s in found or [] if isinstance(s, dict) and "latitude" in s)


def source_tides(place: str) -> str | None:
    """The next highs and lows at the nearest Canadian Hydrographic Service station."""
    hit = _place(place)
    if not hit:
        return None
    stations = tide_stations()
    if not stations:
        return None
    lat, lon = hit["latitude"], hit["longitude"]
    station = min(stations, key=lambda s: km_between(lat, lon, s["latitude"], s["longitude"]))
    away = km_between(lat, lon, station["latitude"], station["longitude"])
    if away > TIDE_RANGE_KM:
        return f"There's no Canadian tide station within {TIDE_RANGE_KM}km of {place_label(hit)}."

    now = _now().replace(microsecond=0)
    events = net.get_json(
        f"{IWLS}/stations/{station['id']}/data",
        "the tide service",
        params={
            "time-series-code": "wlp-hilo",
            "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (now + timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    events = [e for e in events or [] if isinstance(e, dict) and "value" in e]
    if len(events) < 2:  # a 25-hour window always has three or four, so this is a gap
        return None
    zone = ZoneInfo(hit.get("timezone") or "UTC")
    today = now.astimezone(zone).date()
    tides = []
    for i, event in enumerate(events):
        # The series is only times and heights, so a high is one above its neighbour.
        other = events[i + 1] if i + 1 < len(events) else events[i - 1]
        kind = "high" if event["value"] > other["value"] else "low"
        when = _parse_time(event["eventDate"]).astimezone(zone)
        day = "" if when.date() == today else f"{when:%a} "
        tides.append(f"{kind} {day}{when:%H:%M} {event['value']:.1f}m")
    where = station.get("officialName", "the nearest station")
    if away >= 5:
        where += f" ({away:.0f}km away)"
    return f"{where}: {', '.join(tides)} ({now.astimezone(zone):%Z})"


def source_avalanche(place: str) -> str | None:
    """Today's danger ratings and problems from Avalanche Canada's forecast for the spot."""
    hit = _place(place)
    if not hit:
        return None
    data = net.get_json(
        "https://api.avalanche.ca/forecasts/en/products/point",
        "Avalanche Canada",
        params={"lat": hit["latitude"], "long": hit["longitude"]},
    )
    if not isinstance(data, dict) or not data.get("id"):
        return f"Avalanche Canada has no forecast covering {place_label(hit)}."
    report = data.get("report") or {}
    highlights = html_to_text(report.get("highlights") or "")
    ratings = (report.get("dangerRatings") or [{}])[0]
    levels = {
        label: (ratings.get("ratings") or {}).get(key, {}).get("rating") or {}
        for key, label in (("alp", "alpine"), ("tln", "treeline"), ("btl", "below treeline"))
    }
    # A rating reads "3 - Considerable", and the dash is a wasted character in an SMS.
    shown = [
        f"{label} {str(r.get('display', '?')).replace(' - ', ' ')}" for label, r in levels.items()
    ]
    if {r.get("value") for r in levels.values()} <= {"offseason", None}:
        return f"Avalanche Canada: {highlights}" if highlights else None
    # Out of season the title lists every region in BC, so only a short one is used.
    title = report.get("title") or ""
    head = f"Avalanche Canada, {title}" if 0 < len(title) <= 40 else "Avalanche Canada"
    day = (ratings.get("date") or {}).get("display")
    parts = [f"{head}{', ' + day if day else ''}: {', '.join(shown)}"]
    problems = [
        (p.get("type") or {}).get("display") for p in report.get("problems") or [] if p.get("type")
    ]
    if problems:
        parts.append(f"Problems: {', '.join(p for p in problems if p)}")
    if highlights:
        parts.append(highlights)
    return ". ".join(parts)


# What people call BC highways, as opposed to the numbers DriveBC files them under.
HIGHWAY_NAMES = {
    "sea to sky": "99",
    "duffey lake": "99",
    "coquihalla": "5",
    "coq": "5",
    "trans canada": "1",
    "trans-canada": "1",
    "malahat": "1",
    "crowsnest": "3",
    "hope princeton": "3",
    "hope-princeton": "3",
    "yellowhead": "16",
    "okanagan connector": "97C",
    "connector": "97C",
}
LAST_UPDATE = re.compile(r"\s*(?:Last|Next) update: [^.]*\.")


def _road_filter(text: str) -> tuple[str, dict[str, Any]] | None:
    """Turn "hwy 99", "coquihalla" or a BC town into a label and an Open511 filter."""
    name = text.lower().strip(" .")
    number = re.fullmatch(r"(?:hwy|highway|route|rte)?\s*#?\s*(\d{1,3}[a-z]?)", name)
    if number or name in HIGHWAY_NAMES:
        road = number.group(1).upper() if number else HIGHWAY_NAMES[name]
        return f"Highway {road}", {"road_name": f"Highway {road}"}
    hit = _place(text)
    if not hit:
        return None
    if hit.get("admin1") != "British Columbia":
        return place_label(hit), {}
    lat, lon = hit["latitude"], hit["longitude"]
    box = f"{lon - 0.3:.4f},{lat - 0.2:.4f},{lon + 0.3:.4f},{lat + 0.2:.4f}"
    return f"near {hit['name']}", {"bbox": box}


def _event_order(event: dict[str, Any]) -> tuple[bool, bool, bool]:
    text = str(event.get("description", "")).lower()
    return (
        event.get("event_type") != "INCIDENT",
        event.get("severity") != "MAJOR",
        "closed" not in text,
    )


def source_roads(text: str) -> str | None:
    """DriveBC's current events on a BC highway or near a BC town, incidents and closures first."""
    found = _road_filter(text)
    if not found:
        return None
    label, filters = found
    if not filters:
        return f"DriveBC only covers BC roads, and {label} isn't in BC."
    data = net.get_json(
        "https://api.open511.gov.bc.ca/events",
        "DriveBC",
        params={"format": "json", "status": "ACTIVE", "limit": 100, **filters},
    )
    events = sorted((data or {}).get("events") or [], key=_event_order)
    if not events:
        return f"No DriveBC events {label.replace('Highway', 'on Highway')} right now."
    notes = [LAST_UPDATE.sub("", str(e.get("description", ""))).strip() for e in events]
    count = f"{len(events)} event{'s' if len(events) != 1 else ''}"
    return f"DriveBC, {label}, {count}: " + " / ".join(n for n in notes if n)


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def source_drive(text: str) -> str | None:
    """Driving distance and time between two places from OSRM's public router."""
    if " to " not in text:
        return None
    start, end = (p.strip() for p in text.removeprefix("from ").rsplit(" to ", 1))
    a, b = _place(start), _place(end)
    if not a or not b:
        return None
    route = net.get_json(
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{a['longitude']},{a['latitude']};{b['longitude']},{b['latitude']}",
        "the OSRM router",
        params={"overview": "false"},
        gap=1.0,  # the public server allows one request a second
    )
    routes = (route or {}).get("routes") or []
    waypoints = (route or {}).get("waypoints") or []
    # With no road nearby OSRM snaps to the closest one it has, which can be an ocean away.
    if not routes or any(w.get("distance", 0) > 5000 for w in waypoints):
        return f"There's no road route from {place_label(a)} to {place_label(b)}."
    best = routes[0]
    return (
        f"{place_label(a)} to {place_label(b)}: {best['distance'] / 1000:.0f}km, "
        f"about {_duration(best['duration'])} driving (OSRM, no traffic or closures)"
    )
