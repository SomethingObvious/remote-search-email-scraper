"""HTTP for every source, with one timeout, bounded retries and one kind of failure."""

import logging
import threading
import time
from functools import cache
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import TimeoutError as Urllib3Timeout
from urllib3.util.retry import Retry

logger = logging.getLogger("remotesearch")

REQUEST_TIMEOUT = 10  # seconds, so one stuck API can't hang the poll loop
REPO_URL = "https://github.com/SomethingObvious/remote-search-sms"
# Wikimedia's User-Agent policy asks for this shape, with a way to reach whoever runs
# it in the brackets, and it may block a script that sends the requests default.
USER_AGENT = f"RemoteSearch/4.0 ({REPO_URL}) python-requests/{requests.__version__}"


class SourceError(Exception):
    """A source couldn't be reached, or sent back something that can't be read."""

    def __init__(self, name: str, why: str) -> None:
        super().__init__(f"{name}: {why}")
        self.name = name
        self.why = why


@cache
def session() -> requests.Session:
    """One pooled session for the process, retrying 429 and 5xx twice."""
    sess = requests.Session()
    # Retry-After is ignored because urllib3 will sleep up to 6 hours on it, and every
    # reply after that would wait behind the one rate-limited API.
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=False,
        raise_on_status=False,  # the last 429 or 503 comes back, so the reply can name it
    )
    sess.mount("https://", HTTPAdapter(max_retries=retry))
    sess.headers["User-Agent"] = USER_AGENT
    return sess


_last_call: dict[str, float] = {}
_spacing = threading.Lock()


def space_out(host: str, gap: float) -> None:
    """Wait until ``gap`` seconds have passed since the last call to ``host``."""
    with _spacing:
        wait = _last_call.get(host, float("-inf")) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


def fetch(
    url: str,
    name: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    gap: float = 0,
) -> requests.Response | None:
    """GET ``url``, returning None on a 404 and raising SourceError on any other failure.

    A 404 is how the dictionary and currency APIs say they don't know the word, so it
    means "no answer" rather than "down". ``gap`` spaces out calls to hosts whose usage
    policy asks for it, like Nominatim's one request a second.
    """
    if gap:
        space_out(urlsplit(url).netloc, gap)
    try:
        resp = session().get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Couldn't reach %s (%s)", url, exc)
        # Once the retries run out, a timeout arrives wrapped in a ConnectionError.
        reason = getattr(exc.args[0], "reason", None) if exc.args else None
        timed_out = isinstance(exc, requests.Timeout) or isinstance(reason, Urllib3Timeout)
        raise SourceError(name, "it timed out" if timed_out else "couldn't connect") from None
    if resp.status_code == 404:
        return None
    if resp.status_code == 429:
        raise SourceError(name, "it's limiting requests")
    if not resp.ok:
        logger.warning("%s answered %s for %s", name, resp.status_code, url)
        raise SourceError(name, f"it answered with error {resp.status_code}")
    return resp


def get_json(url: str, name: str, **kwargs: Any) -> Any:
    """GET and parse JSON, with None for a 404. See fetch() for the rest."""
    resp = fetch(url, name, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        raise SourceError(name, "its reply wasn't readable") from None


def get_text(url: str, name: str, **kwargs: Any) -> str | None:
    resp = fetch(url, name, **kwargs)
    return None if resp is None else resp.text
