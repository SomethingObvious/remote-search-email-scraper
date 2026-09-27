"""Answer questions texted from a phone that has signal but no data.

A carrier's SMS-to-email gateway drops each text into a Gmail label. This reads the
label, looks each question up in a few free web APIs and texts the answer to PHONE_TO
through Twilio. ``python RemoteSearch.py --query "sun Tofino"`` tries a lookup without
any accounts.
"""

import argparse
import base64
import html
import logging
import os
import re
import unicodedata
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from email.utils import parseaddr
from functools import cache
from pathlib import Path
from time import sleep
from typing import Any

import requests
from bs4 import BeautifulSoup
from google.auth.exceptions import RefreshError
from requests.adapters import HTTPAdapter
from twilio.base.exceptions import TwilioException
from urllib3.util.retry import Retry

logger = logging.getLogger("remotesearch")

REQUEST_TIMEOUT = 10  # seconds, so one stuck API can't hang the poll loop
USER_AGENT = "RemoteSearch/3.1 (+https://github.com/SomethingObvious/remote-search-email-scraper)"
DEFAULT_SMS_CHARS = 300  # two GSM-7 segments hold 306
TWILIO_MAX_CHARS = 1600  # Twilio rejects a longer body outright
MAX_QUERY_CHARS = 200  # anything longer is a signature or boilerplate, not a question
DEFAULT_MAX_REPLIES = 10  # per poll, and every reply is a billed SMS
DEFAULT_INTERVAL = 5
DEFAULT_LABEL = "Remote Server"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# Marking mail read needs one of these, and a read-only scope fails on every message.
MODIFY_SCOPES = (DEFAULT_SCOPE, "https://mail.google.com/")

GMAIL_KEYS = ("GMAIL_CREDENTIALS_FILE", "GMAIL_TOKEN_FILE")
TWILIO_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_FROM", "PHONE_TO")
OPTIONAL_KEYS = (
    "GMAIL_SCOPE",
    "LABEL_NAME",
    "POLL_INTERVAL",
    "MAX_SMS_CHARS",
    "ALLOWED_SENDERS",
    "MAX_REPLIES_PER_POLL",
)


def load_config(path: str) -> dict[str, str]:
    """Read ``KEY=value`` lines from ``path`` if it exists, with environment variables winning."""
    config: dict[str, str] = {}
    file = Path(path)
    if file.exists():
        for raw in file.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    for key in (*GMAIL_KEYS, *TWILIO_KEYS, *OPTIONAL_KEYS):
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def require(config: dict[str, str], keys: tuple[str, ...], path: str) -> None:
    missing = [k for k in keys if not config.get(k)]
    if missing:
        raise SystemExit(
            f"Neither {path} nor the environment sets {', '.join(missing)}. "
            "config.example.txt shows what each one is."
        )


def setting(config: dict[str, str], key: str, default: int) -> int:
    """Read a config value that has to be a whole number of 1 or more."""
    raw = config.get(key) or str(default)
    if not raw.isdecimal() or int(raw) < 1:
        raise SystemExit(f"{key} has to be a whole number of 1 or more, not {raw!r}.")
    return int(raw)


def allowed_senders(config: dict[str, str]) -> set[str]:
    """Parse ALLOWED_SENDERS into a lowercase set of addresses and bare domains."""
    raw = config.get("ALLOWED_SENDERS", "")
    return {part.strip().lower().lstrip("@") for part in raw.split(",") if part.strip()}


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
    )
    sess.mount("https://", HTTPAdapter(max_retries=retry))
    sess.headers["User-Agent"] = USER_AGENT
    return sess


def get_json(url: str, **params: Any) -> Any:
    """GET and parse JSON, or None on any network, HTTP or parse error."""
    try:
        resp = session().get(url, params=params or None, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Couldn't get %s (%s)", url, exc)
        return None


def run_source(source: Callable[[str], str | None], arg: str) -> str | None:
    """Call a source, turning any error into None so the reply falls back to a web search."""
    try:
        return source(arg)
    except Exception as exc:  # a bug in one source still leaves the web search to answer
        logger.warning("%s failed on %r: %s", getattr(source, "__name__", source), arg, exc)
        return None


# Rogers puts "Rogers MMS" in front of the text itself, but a bare "Rogers" only
# counts as noise on its own line, or "who is Fred Rogers" would lose its answer.
CARRIER_NOISE = re.compile(
    r"Rogers MMS|^\s*Rogers\s*$|This message is brought to you by[^\n]*|Sent from my \w+",
    re.IGNORECASE | re.MULTILINE,
)


def clean_query(text: str) -> str:
    """Drop carrier boilerplate and collapse the rest to one line."""
    text = CARRIER_NOISE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(markup: str) -> str:
    return clean_query(BeautifulSoup(markup, "html.parser").get_text("\n"))


def strip_refs(text: str) -> str:
    """Remove reference markers like [2] or [note] from prose."""
    return re.sub(r"\[[A-Za-z0-9]+\]", "", text).strip()


def truncate(text: str, limit: int) -> str:
    """Trim to at most ``limit`` characters on a word boundary, ending in a plain '...'."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    if limit <= 3:  # no room for the dots, and text[:limit - 3] would slice from the end
        return text[: max(limit, 0)]
    cut = text[: limit - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "..."


# One character outside GSM-7 makes Twilio send the whole reply as UCS-2, where a
# segment holds 67 characters instead of 153, so a 300-character answer bills as 5.
SMS_PUNCTUATION = str.maketrans(
    {
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{MINUS SIGN}": "-",
        "`": "'",
    }
)


def plain_ascii(text: str) -> str:
    """Fold text to plain ASCII, dropping accents and anything with no ASCII form."""
    text = text.translate(SMS_PUNCTUATION)
    # Every character Unicode files as dash punctuation (category Pd) becomes a hyphen.
    text = "".join("-" if unicodedata.category(c) == "Pd" else c for c in text)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


# Words that carry no topic, so they never count as evidence that a result matches.
STOPWORDS = frozenset(
    """a an and are as at be been but by can did do does for from had has have how i if in
    is it its me my of on or that the their there these they this to was were what when
    where which who why will with you your""".split()  # noqa: SIM905 - reads better as prose
)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def content_words(text: str) -> set[str]:
    """Lowercase words of three or more letters that aren't stopwords."""
    return {w for w in words(text) if len(w) >= 3 and w not in STOPWORDS}


def _shares_stem(a: str, b: str) -> bool:
    if a == b:
        return True
    common = len(os.path.commonprefix([a, b]))
    return common >= 4  # purify and purified, boil and boiler, but not hike and hiking


def looks_relevant(query: str, title: str) -> bool:
    """True when a result's title overlaps what was asked.

    Wikipedia's search returns a hit for any query built from real words, so without
    this "how do I treat a blister" gets a summary of a memoir that mentions blisters.
    On a trail with no data, a confident wrong answer is worse than none.
    """
    # A query made only of short words ("AC/DC", "UV") still has to match something.
    asked = content_words(query) or set(words(query))
    return any(_shares_stem(t, q) for t in words(title) for q in asked)


def source_duckduckgo(query: str) -> str | None:
    """DuckDuckGo's Instant Answer, which only covers direct answers and topic abstracts."""
    data = get_json(
        "https://api.duckduckgo.com/",
        q=query,
        format="json",
        no_html="1",
        skip_disambig="1",
    )
    if not data:
        return None
    for key in ("Answer", "AbstractText", "Definition"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Related topics come back for nearly anything, so they get the same title check
    # as Wikipedia. The title is the last part of the topic's URL.
    for topic in data.get("RelatedTopics", []):
        if not isinstance(topic, dict) or not topic.get("Text"):
            continue
        title = urllib.parse.unquote(str(topic.get("FirstURL", "")).rsplit("/", 1)[-1])
        if looks_relevant(query, title.replace("_", " ")):
            return str(topic["Text"]).strip()
    return None


def source_wikipedia(query: str) -> str | None:
    """Top Wikipedia hit's lead summary, named and checked for relevance.

    The article title goes in the reply on purpose. It costs a few characters, and it's
    the only way the reader can tell a real answer from a near miss.
    """
    hits = get_json(
        "https://en.wikipedia.org/w/api.php",
        action="query",
        list="search",
        srsearch=query,
        srlimit="1",
        format="json",
    )
    results = (hits or {}).get("query", {}).get("search", [])
    if not results:
        return None

    title = results[0]["title"]
    if not looks_relevant(query, title):
        logger.info("Skipping Wikipedia's hit %r for %r as the title doesn't match", title, query)
        return None

    # safe="" matters for titles like "AC/DC", where a bare slash splits the path.
    path = urllib.parse.quote(title.replace(" ", "_"), safe="")
    summary = get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{path}")
    if not summary or summary.get("type") == "disambiguation" or not summary.get("extract"):
        return None
    text = strip_refs(summary["extract"])
    # Most lead sentences open with the article name, so it's only added when they
    # don't. Every repeated character is one fewer character of answer.
    return text if text.lower().startswith(title.lower()) else f"{title}: {text}"


def source_dictionary(word: str) -> str | None:
    """First one or two senses from the free Dictionary API."""
    entries = get_json(
        f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word, safe='')}"
    )
    if not isinstance(entries, list) or not entries:
        return None
    senses = []
    for meaning in entries[0].get("meanings", [])[:2]:
        definitions = meaning.get("definitions", [])
        if definitions:
            pos = meaning.get("partOfSpeech", "")
            senses.append(f"({pos}) {definitions[0].get('definition', '')}".strip())
    return " ".join(senses) or None


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


def geocode(place: str) -> dict[str, Any] | None:
    """Resolve a place name to coordinates through Open-Meteo's keyless geocoder."""
    data = get_json(
        "https://geocoding-api.open-meteo.com/v1/search", name=place, count=1, format="json"
    )
    results = (data or {}).get("results") or []
    return results[0] if results else None


def place_label(hit: dict[str, Any]) -> str:
    """Name a geocoded place closely enough that a wrong match is obvious."""
    parts = [hit.get("name"), hit.get("admin1"), hit.get("country_code")]
    return ", ".join(str(p) for p in parts if p)


def _forecast(hit: dict[str, Any], **fields: str) -> dict[str, Any] | None:
    return get_json(
        "https://api.open-meteo.com/v1/forecast",
        latitude=hit["latitude"],
        longitude=hit["longitude"],
        timezone="auto",
        forecast_days=1,
        **fields,
    )


def source_weather(place: str) -> str | None:
    """Current conditions from Open-Meteo, with C and km/h spelled out for SMS."""
    hit = geocode(place)
    if not hit:
        return None
    data = _forecast(
        hit,
        current="temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
    )
    now = (data or {}).get("current")
    if not now:
        return None
    return (
        f"{place_label(hit)}: {WMO_CODES.get(now.get('weather_code'), 'unknown')}, "
        f"{now['temperature_2m']:.0f}C (feels {now['apparent_temperature']:.0f}C), "
        f"wind {now['wind_speed_10m']:.0f}km/h, humidity {now['relative_humidity_2m']:.0f}%"
    )


def source_sun(place: str) -> str | None:
    """Today's sunrise and sunset in the place's own timezone."""
    hit = geocode(place)
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
    return (
        f"{place_label(hit)}: sunrise {sunrise[11:16]}, sunset {sunset[11:16]} "
        f"({data.get('timezone_abbreviation', 'local')})"
    )


def source_stackoverflow(query: str) -> str | None:
    """Top Stack Overflow question plus its highest-voted answer."""
    found = get_json(
        "https://api.stackexchange.com/2.3/search/advanced",
        order="desc",
        sort="relevance",
        q=query,
        site="stackoverflow",
        pagesize="1",
    )
    items = (found or {}).get("items", [])
    if not items:
        return None
    question = items[0]
    # Stack Exchange sends titles HTML-escaped, so "&quot;" would go out in the text.
    title = html.unescape(question.get("title", ""))
    question_id = question.get("question_id")
    if not question_id:
        return title or None
    answers = get_json(
        f"https://api.stackexchange.com/2.3/questions/{question_id}/answers",
        order="desc",
        sort="votes",
        site="stackoverflow",
        pagesize="1",
        filter="withbody",
    )
    body_items = (answers or {}).get("items", [])
    if not body_items:
        return f"{title} (no answers yet)"
    body = BeautifulSoup(body_items[0].get("body", ""), "html.parser").get_text(" ", strip=True)
    # The space get_text puts between tags also lands before punctuation after </code>.
    body = re.sub(r"\s+([.,;:!?)])", r"\1", body)
    if not title.endswith(("?", ".", "!")):
        title += "."
    return f"{title} {body}"


ROUTES: dict[str, Callable[[str], str | None]] = {
    "weather": source_weather,
    "sun": source_sun,
    "sunset": source_sun,
    "sunrise": source_sun,
    "define": source_dictionary,
    "def": source_dictionary,
    "dict": source_dictionary,
    "wiki": source_wikipedia,
    "so": source_stackoverflow,
    "stack": source_stackoverflow,
    "stackoverflow": source_stackoverflow,
}
HELP_WORDS = {"help", "?", "commands"}
HELP_TEXT = (
    "Start a text with weather, sun, define, wiki or so and then what you want, "
    "like 'sun Tofino'. Anything else gets a web search."
)
EMPTY_TEXT = "Your text came through empty. Text help to see the commands."
NO_RESULT_TEXT = (
    "Couldn't find a good answer for '{target}'. Try a keyword instead of a whole "
    "question, or text help for the commands."
)
ONLINE_TEXT = "Remote search is running. Text help to see the commands."


def default_search(query: str, skip: Callable[[str], str | None] | None = None) -> str | None:
    """Ask DuckDuckGo and Wikipedia at once and prefer DuckDuckGo, leaving out ``skip``."""
    sources = [s for s in (source_duckduckgo, source_wikipedia) if s is not skip]
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        results = list(pool.map(lambda s: run_source(s, query), sources))
    return next((r for r in results if r), None)


def answer(query: str, limit: int = DEFAULT_SMS_CHARS) -> str:
    """Route one text to a source and format the reply to fit in ``limit`` characters."""
    query = re.sub(r"\s+", " ", query).strip()[:MAX_QUERY_CHARS].strip()
    if not query:
        return truncate(EMPTY_TEXT, limit)

    command, _, rest = query.partition(" ")
    key, rest = command.lower().strip(":,"), rest.strip()
    if key in HELP_WORDS and not rest:  # "help me ..." is a real question
        return truncate(HELP_TEXT, limit)

    source = ROUTES.get(key) if rest else None
    target, tag, result = query, "web", None
    if source:
        target, tag, result = rest, key, run_source(source, rest)
    if not result:
        tag, result = "web", default_search(target, skip=source)
    if not result:
        # The question is shortened so the advice after it still fits in the reply.
        return truncate(plain_ascii(NO_RESULT_TEXT.format(target=truncate(target, 60))), limit)
    return truncate(plain_ascii(f"{tag}: {result}"), limit)


def authenticate_gmail(config: dict[str, str]) -> Any:
    """Build a Gmail client, asking for a browser login only when the saved one can't be used."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = [config.get("GMAIL_SCOPE") or DEFAULT_SCOPE]
    token = Path(config["GMAIL_TOKEN_FILE"])
    # Loaded with the scopes it was granted, so a token saved under a narrower scope
    # is replaced here instead of failing to mark each message read.
    creds = Credentials.from_authorized_user_file(str(token)) if token.exists() else None
    if creds and not creds.has_scopes(scopes):
        creds = None
    if creds and creds.valid:
        return build("gmail", "v1", credentials=creds)
    if creds:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            logger.warning("The saved Gmail login stopped working (%s), so it needs a new one", exc)
            creds = None
    if not creds:
        secrets = config["GMAIL_CREDENTIALS_FILE"]
        if not Path(secrets).exists():
            raise SystemExit(
                f"Couldn't find {secrets}. Download the Desktop app OAuth client JSON from the "
                "Google Cloud console and point GMAIL_CREDENTIALS_FILE at it."
            )
        creds = InstalledAppFlow.from_client_secrets_file(secrets, scopes).run_local_server(port=0)
    # It holds a refresh token, so nobody else on the machine should be able to read it.
    token.touch(mode=0o600)
    token.chmod(0o600)
    token.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def get_label_id(service: Any, label_name: str) -> str | None:
    """Resolve a Gmail label's display name to its ID, ignoring case."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == label_name.lower():
            return str(label["id"])
    return None


def _find_body(part: dict[str, Any], mime: str) -> str | None:
    """Walk a possibly nested multipart payload for the first body of type ``mime``."""
    data = part.get("body", {}).get("data")
    if part.get("mimeType") == mime and data:
        # urlsafe_b64decode refuses data without its = padding, so it's padded here in
        # case Gmail leaves it off.
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    for sub in part.get("parts") or []:
        found = _find_body(sub, mime)
        if found:
            return found
    return None


def extract_query(message: dict[str, Any]) -> str | None:
    """Pull the text out of a message, preferring plain text over HTML."""
    payload = message.get("payload", {})
    plain = _find_body(payload, "text/plain")
    if plain:
        return clean_query(plain)
    markup = _find_body(payload, "text/html")
    return html_to_text(markup) if markup else None


def sender_address(message: dict[str, Any]) -> str:
    """The From address, lowercased, or an empty string when there isn't one clear address."""
    headers = message.get("payload", {}).get("headers", [])
    value = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "from"), "")
    # parseaddr takes the address in the angle brackets. A plain regex takes the first
    # thing shaped like an address, which can be a fake one in the display name.
    address = parseaddr(value)[1].lower()
    return address if "@" in address else ""


def sender_permitted(address: str, allowed: set[str]) -> bool:
    """Match a sender against the allowlist by full address or by domain.

    An empty allowlist lets everyone through, which keeps an install from before the
    setting existed working. main() warns about it at startup.
    """
    if not allowed:
        return True
    domain = address.rpartition("@")[2]
    return address in allowed or (bool(domain) and domain in allowed)


def unread_ids(service: Any, label_id: str) -> list[str]:
    """IDs of unread messages under the label, oldest first, across every page."""
    ids: list[str] = []
    token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", labelIds=[label_id, "UNREAD"], maxResults=500, pageToken=token)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            return list(reversed(ids))


def mark_read(service: Any, ids: list[str]) -> None:
    for start in range(0, len(ids), 1000):  # batchModify takes up to 1000 IDs a call
        body = {"ids": ids[start : start + 1000], "removeLabelIds": ["UNREAD"]}
        service.users().messages().batchModify(userId="me", body=body).execute()


def make_sender(config: dict[str, str], dry_run: bool) -> Callable[[str], bool]:
    """Return ``send(text) -> delivered``, which only logs the text on a dry run."""
    if dry_run:

        def pretend(text: str) -> bool:
            logger.info("Dry run, so not texting: %s", text)
            return True

        return pretend

    from twilio.http.http_client import TwilioHttpClient
    from twilio.rest import Client

    # Twilio's client waits forever by default, and one stuck send would stop the poll loop.
    client = Client(
        config["TWILIO_ACCOUNT_SID"],
        config["TWILIO_AUTH_TOKEN"],
        http_client=TwilioHttpClient(timeout=REQUEST_TIMEOUT),
    )
    to, from_ = config["PHONE_TO"], config["TWILIO_PHONE_FROM"]

    def send(text: str) -> bool:
        try:
            sms = client.messages.create(to=to, from_=from_, body=text)
        except (TwilioException, requests.RequestException) as exc:
            logger.error("Couldn't send the SMS: %s", exc)
            return False
        logger.debug("Sent %s", sms.sid)
        return True

    return send


def process_once(
    service: Any,
    label_id: str,
    send: Callable[[str], bool],
    limit: int,
    *,
    allowed: set[str] | None = None,
    max_replies: int = DEFAULT_MAX_REPLIES,
) -> int:
    """Answer unread mail under the label, up to ``max_replies``, and return how many went out."""
    replied = 0
    for msg_id in unread_ids(service, label_id):
        if replied >= max_replies:
            logger.warning(
                "Sent %d replies this poll, which is the cap, so the rest wait for the next one",
                max_replies,
            )
            break
        try:
            message = service.users().messages().get(userId="me", id=msg_id).execute()
            # Marked read before the reply goes out. The other way round, a message that
            # can't be marked gets answered (and billed) again on every poll after.
            mark_read(service, [msg_id])
            sender = sender_address(message)
            query = extract_query(message)
            if not sender_permitted(sender, allowed or set()):
                logger.warning("Ignoring mail from %r as it isn't in ALLOWED_SENDERS", sender)
            elif not query:
                logger.warning("Message %s had no text to answer", msg_id)
            else:
                logger.info("Question from %s: %s", sender or "an unknown sender", query)
                if send(answer(query, limit)):
                    replied += 1
        except RefreshError:
            raise
        except Exception as exc:  # one bad message shouldn't hold up the rest of the batch
            logger.error("Couldn't handle message %s: %s", msg_id, exc)
    return replied


def monitor(
    service: Any,
    label_id: str,
    send: Callable[[str], bool],
    *,
    limit: int,
    interval: int,
    catch_up: bool,
    allowed: set[str] | None = None,
    max_replies: int = DEFAULT_MAX_REPLIES,
) -> None:
    """Poll forever, skipping the mail already waiting at startup unless ``catch_up`` is set."""
    if not catch_up:
        mark_read(service, unread_ids(service, label_id))
        send(ONLINE_TEXT)

    failures = 0
    while True:
        try:
            process_once(service, label_id, send, limit, allowed=allowed, max_replies=max_replies)
            failures = 0
        except RefreshError:
            raise  # retrying can't fix a revoked login, so main() explains it and exits
        except Exception as exc:  # Gmail has short outages, and one shouldn't stop the service
            failures += 1
            logger.error("Poll failed (%d in a row): %s", failures, exc)
        # It backs off to 5 minutes while Gmail keeps failing instead of hammering it.
        sleep(max(interval, min(interval * 2**failures, 300)))


def positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"has to be 1 or more, not {value}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Answer questions texted in through Gmail by SMS.")
    parser.add_argument("--config", default="config.txt", help="path to the config file")
    parser.add_argument("--query", help="answer one question and exit, with no accounts needed")
    parser.add_argument("--once", action="store_true", help="answer the unread mail and exit")
    parser.add_argument("--catch-up", action="store_true", help="answer mail waiting at startup")
    parser.add_argument("--interval", type=positive, help="seconds between polls")
    parser.add_argument("--max-chars", type=positive, help="longest reply in characters")
    parser.add_argument("--max-replies", type=positive, help="most texts sent per poll")
    parser.add_argument("--dry-run", action="store_true", help="log replies instead of texting")
    parser.add_argument("--verbose", action="store_true", help="log debug detail")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # Only this script logs at INFO. Twilio's client logs every request at INFO,
    # account SID included.
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    if args.query is not None:
        print(answer(args.query, args.max_chars or DEFAULT_SMS_CHARS))
        return

    config = load_config(args.config)
    require(config, GMAIL_KEYS if args.dry_run else GMAIL_KEYS + TWILIO_KEYS, args.config)
    scope = config.get("GMAIL_SCOPE") or DEFAULT_SCOPE
    if scope not in MODIFY_SCOPES:
        raise SystemExit(
            f"GMAIL_SCOPE is {scope}, which can't mark mail read. Use {DEFAULT_SCOPE}."
        )
    limit = args.max_chars or setting(config, "MAX_SMS_CHARS", DEFAULT_SMS_CHARS)
    if limit > TWILIO_MAX_CHARS:
        raise SystemExit(
            f"The reply limit is {limit} characters, but Twilio won't send more than "
            f"{TWILIO_MAX_CHARS}. Lower MAX_SMS_CHARS or --max-chars."
        )
    interval = args.interval or setting(config, "POLL_INTERVAL", DEFAULT_INTERVAL)
    max_replies = args.max_replies or setting(config, "MAX_REPLIES_PER_POLL", DEFAULT_MAX_REPLIES)

    allowed = allowed_senders(config)
    if allowed:
        logger.info("Answering mail from %s only", ", ".join(sorted(allowed)))
    else:
        logger.warning(
            "ALLOWED_SENDERS is empty, so any mail that reaches the label gets a billed SMS "
            "in reply. Set it to your carrier's gateway domain."
        )

    service = authenticate_gmail(config)
    label_name = config.get("LABEL_NAME") or DEFAULT_LABEL
    label_id = get_label_id(service, label_name)
    if not label_id:
        raise SystemExit(
            f"There's no Gmail label called {label_name!r}. Create it, or set LABEL_NAME "
            "to the label your gateway filter applies."
        )

    send = make_sender(config, args.dry_run)
    try:
        if args.once:
            count = process_once(
                service, label_id, send, limit, allowed=allowed, max_replies=max_replies
            )
            logger.info("Sent %d %s", count, "reply" if count == 1 else "replies")
        else:
            logger.info("Checking the %r label every %d seconds", label_name, interval)
            monitor(
                service,
                label_id,
                send,
                limit=limit,
                interval=interval,
                catch_up=args.catch_up,
                allowed=allowed,
                max_replies=max_replies,
            )
    except RefreshError as exc:
        raise SystemExit(
            f"Gmail turned down the saved login ({exc}). Delete {config['GMAIL_TOKEN_FILE']} "
            "and run this again to log back in."
        ) from None
    except KeyboardInterrupt:
        pass  # Ctrl-C is how it's meant to be stopped


if __name__ == "__main__":
    main()
