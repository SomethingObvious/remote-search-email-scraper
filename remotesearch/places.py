"""Finding places: towns through Open-Meteo, businesses through OpenStreetMap, and local time."""

from datetime import datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from . import net
from .lookup import looks_relevant

LEADING_PREPOSITION = ("in ", "at ", "for ", "near ")


def place_words(text: str) -> str:
    """Drop the "in" from "weather in Tofino", since the geocoder wants just the name."""
    lowered = text.lower()
    for word in LEADING_PREPOSITION:
        if lowered.startswith(word):
            return text[len(word) :].strip()
    return text.strip()


# Towns don't move, so each name is looked up once per run.
@lru_cache(maxsize=256)
def geocode(place: str) -> dict[str, Any] | None:
    """Resolve a place name to coordinates through Open-Meteo's keyless geocoder."""
    data = net.get_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        "Open-Meteo's geocoder",
        params={"name": place, "count": 1, "format": "json"},
    )
    results = (data or {}).get("results") or []
    return results[0] if results else None


def place_label(hit: dict[str, Any]) -> str:
    """Name a geocoded place closely enough that a wrong match is obvious."""
    parts = [hit.get("name"), hit.get("admin1"), hit.get("country_code")]
    return ", ".join(str(p) for p in parts if p)


def source_time(place: str) -> str | None:
    hit = geocode(place_words(place))
    if not hit or not hit.get("timezone"):
        return None
    now = datetime.now(ZoneInfo(hit["timezone"]))
    return f"{place_label(hit)}: {now:%H:%M %a %b} {now.day} ({now:%Z})"


# Nominatim's usage policy asks for no more than one request a second, and for
# results to be kept rather than fetched again.
@lru_cache(maxsize=128)
def _nominatim(query: str) -> tuple[dict[str, Any], ...]:
    found = net.get_json(
        "https://nominatim.openstreetmap.org/search",
        "OpenStreetMap's Nominatim",
        params={"q": query, "format": "jsonv2", "extratags": 1, "addressdetails": 1, "limit": 3},
        gap=1.0,
    )
    return tuple(r for r in found or [] if isinstance(r, dict))


def _street_address(address: dict[str, str]) -> str:
    street = " ".join(p for p in (address.get("house_number"), address.get("road")) if p)
    town = next((address[k] for k in ("city", "town", "village", "hamlet") if k in address), "")
    return ", ".join(p for p in (street, town) if p)


def source_business(query: str) -> str | None:
    """Opening hours, address and phone number for a named place from OpenStreetMap."""
    # Nominatim reads "Tim Hortons, Hope" better than "Tim Hortons in Hope".
    wanted = query.replace(" in ", ", ").replace(" near ", ", ")
    for hit in _nominatim(wanted):
        name = hit.get("name") or ""
        # A town or a boundary matching the town in the question is no use for its hours.
        if hit.get("category") in ("boundary", "place") or not name:
            continue
        if not looks_relevant(wanted.split(",")[0], name):
            continue
        tags = hit.get("extratags") or {}
        parts = [f"{name}, {_street_address(hit.get('address') or {})}".rstrip(", ")]
        hours = tags.get("opening_hours")
        parts.append(f"Open {hours}" if hours else "No hours listed")
        phone = tags.get("phone") or tags.get("contact:phone")
        if phone:
            parts.append(phone)
        return ". ".join(parts)
    return None
