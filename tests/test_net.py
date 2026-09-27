from typing import Any
from unittest.mock import patch

import pytest
import requests
from conftest import real_session
from requests.adapters import HTTPAdapter
from urllib3.exceptions import MaxRetryError, ReadTimeoutError

from remotesearch import net
from remotesearch.net import SourceError, get_json, get_text


def response(status: int, body: bytes = b"{}") -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    return resp


class FakeSession:
    def __init__(self, reply: requests.Response | Exception) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def fetch_with(reply: requests.Response | Exception, **kwargs: Any) -> Any:
    fake = FakeSession(reply)
    with patch("remotesearch.net.session", return_value=fake):
        return get_json("https://example.com/api", "Example", **kwargs), fake


def test_get_json_passes_params_headers_and_a_timeout() -> None:
    data, fake = fetch_with(response(200, b'{"ok": 1}'), params={"q": "x"}, headers={"H": "v"})
    assert data == {"ok": 1}
    [call] = fake.calls
    assert call == {
        "url": "https://example.com/api",
        "params": {"q": "x"},
        "headers": {"H": "v"},
        "timeout": 10,
    }


def test_a_404_means_no_answer_rather_than_down() -> None:
    assert fetch_with(response(404))[0] is None
    with patch("remotesearch.net.session", return_value=FakeSession(response(404))):
        assert get_text("https://example.com/feed", "Example") is None


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        (response(429), "it's limiting requests"),
        (response(503), "it answered with error 503"),
        (response(522), "it answered with error 522"),
        (response(200, b"<html>not json</html>"), "its reply wasn't readable"),
        (requests.ReadTimeout("slow"), "it timed out"),
        (
            requests.ConnectionError(
                # urllib3 names the pool here, and a real one isn't needed to raise it.
                MaxRetryError(None, "/api", ReadTimeoutError(None, "/api", "timed out"))  # type: ignore[arg-type]
            ),
            "it timed out",
        ),
        (requests.ConnectionError("refused"), "couldn't connect"),
    ],
)
def test_every_failure_names_its_reason(reply: Any, why: str) -> None:
    with pytest.raises(SourceError) as caught:
        fetch_with(reply)
    assert (caught.value.name, caught.value.why) == ("Example", why)


def test_calls_to_a_polite_host_are_spaced_out() -> None:
    fake = FakeSession(response(200))
    with (
        patch("remotesearch.net.session", return_value=fake),
        patch("time.monotonic", side_effect=[100.0, 100.0, 100.4, 101.0]),
        patch("time.sleep") as sleep,
    ):
        get_json("https://nominatim.example/a", "N", gap=1.0)
        get_json("https://nominatim.example/b", "N", gap=1.0)
    sleep.assert_called_once_with(pytest.approx(0.6))


def test_the_session_retries_twice_and_ignores_retry_after() -> None:
    adapter = real_session().get_adapter("https://en.wikipedia.org")
    assert isinstance(adapter, HTTPAdapter)
    retry = adapter.max_retries
    assert retry.respect_retry_after_header is False
    assert retry.total == 2
    assert retry.raise_on_status is False
    assert real_session().headers["User-Agent"] == net.USER_AGENT
