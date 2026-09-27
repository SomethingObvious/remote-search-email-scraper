"""Shared setup. Every test runs offline, and anything that reaches for the network fails.

The files in fixtures/ are real responses recorded once from each keyless source and
trimmed, apart from the made_ ones. Those are written by hand from the provider's
documented shape, since recording them needs a paid key or a different season.
"""

import copy
import json
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import ddgs
import pytest
from ddgs.exceptions import DDGSException

from remotesearch import net, outdoors, places, search

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkUsed(BaseException):
    """Not an Exception, so the catch-alls in the code under test can't swallow it."""


def _no_network(*args: Any, **kwargs: Any) -> Any:
    raise NetworkUsed("a test reached for the real network")


# requests, the Google client and Twilio's client all go through the socket module.
# ddgs does its HTTP in Rust, below it, so its class is blocked by name.
real_session = net.session
net.session = _no_network  # type: ignore[assignment]
socket.socket.connect = _no_network  # type: ignore[method-assign]
socket.create_connection = _no_network
socket.getaddrinfo = _no_network
ddgs.DDGS = _no_network  # type: ignore[misc, assignment]


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    """Clear the per-run caches and the search cooldowns, and make every wait instant."""
    places.geocode.cache_clear()
    places._nominatim.cache_clear()
    outdoors.tide_stations.cache_clear()
    search._resting.clear()
    net._last_call.clear()
    with patch("time.sleep"):
        yield


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


Response = Any | BaseException | Callable[[str, dict[str, Any]], Any]


@contextmanager
def fake_web(responses: dict[str, Response]) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Answer net.get_json and net.get_text from ``responses``, keyed by URL prefix.

    A value is returned as a copy, raised when it's an exception, or called with the
    URL and params when it's a function. Unknown URLs get None, which is what a 404
    gives. Each call's URL and params are recorded in the list this yields.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    def respond(url: str, name: str, **kwargs: Any) -> Any:
        params = kwargs.get("params") or {}
        calls.append((url, params))
        for prefix, value in responses.items():
            if url.startswith(prefix):
                if isinstance(value, BaseException):
                    raise value
                if callable(value):
                    return value(url, params)
                return copy.deepcopy(value)
        return None

    with patch.object(net, "get_json", respond), patch.object(net, "get_text", respond):
        yield calls


class FakeDDGS:
    """Stands in for ddgs.DDGS, handing back ``rows`` or raising each of ``errors`` in turn.

    A backend in ``blocked`` fails every time, the way ddgs reports a block.
    """

    rows: ClassVar[list[dict[str, str]]] = []
    errors: ClassVar[list[Exception]] = []
    blocked: ClassVar[set[str]] = set()
    queries: ClassVar[list[str]] = []
    backends: ClassVar[list[str]] = []

    def __init__(self, timeout: int | None = None) -> None:
        self.timeout = timeout

    def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
        FakeDDGS.queries.append(query)
        FakeDDGS.backends.append(kwargs["backend"])
        if kwargs["backend"] in FakeDDGS.blocked:
            raise DDGSException("No results found.")
        if FakeDDGS.errors:
            raise FakeDDGS.errors.pop(0)
        return copy.deepcopy(FakeDDGS.rows)


@pytest.fixture
def ddg() -> Iterator[type[FakeDDGS]]:
    FakeDDGS.rows = load("ddgs_boil_water.json")
    FakeDDGS.errors = []
    FakeDDGS.blocked = set()
    FakeDDGS.queries = []
    FakeDDGS.backends = []
    with patch("ddgs.DDGS", FakeDDGS):
        yield FakeDDGS


OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
DDG = "https://api.duckduckgo.com/"
