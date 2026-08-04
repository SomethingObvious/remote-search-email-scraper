"""Remote web search over SMS.

A field device with no data connection texts a question. The carrier's SMS-to-email
gateway drops it into a Gmail label. This script watches that label, answers the
question from a handful of free web APIs, and texts the answer back through Twilio.

Text ``help`` to see the commands. Anything without a command prefix runs a plain
web search. Run ``python RemoteSearch.py --query "weather Toronto"`` to try the
lookups from a terminal without any Gmail or Twilio credentials.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache, wraps
from pathlib import Path
from time import sleep
from typing import Any

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("remotesearch")

REQUEST_TIMEOUT = 10  # seconds, so a stuck target can't hang the poll loop
USER_AGENT = "RemoteSearch/3.0 (+https://github.com/SomethingObvious/remote-search-email-scraper)"
DEFAULT_SMS_CHARS = 300  # ~2 GSM-7 segments
MAX_QUERY_CHARS = 200  # a mail body longer than this is boilerplate, not a question
DEFAULT_MAX_REPLIES = 10  # per poll; every reply past this is a Twilio charge

# Keys read from config file and/or environment. Gmail needs the modify scope now
# because the poller marks messages read so it never answers the same text twice.
GMAIL_KEYS = ("GMAIL_CREDENTIALS_FILE", "GMAIL_TOKEN_FILE", "GMAIL_SCOPE")
# GMAIL_SCOPE has a default, so it isn't required; the credentials/token paths are.
GMAIL_REQUIRED = ("GMAIL_CREDENTIALS_FILE", "GMAIL_TOKEN_FILE")
TWILIO_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_FROM", "PHONE_TO")
OPTIONAL_KEYS = (
    "LABEL_NAME",
    "POLL_INTERVAL",
    "MAX_SMS_CHARS",
    "ALLOWED_SENDERS",
    "MAX_REPLIES_PER_POLL",
)
DEFAULT_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: str) -> dict[str, str]:
    """Read ``key=value`` lines from a file, then let environment variables win.

    Both sources are optional: a deployment can ship a config file, use env vars
    (e.g. a systemd unit or a secrets manager), or mix the two.
    """
    config: dict[str, str] = {}
    file = Path(path)
    if file.exists():
        for raw in file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    for key in (*GMAIL_KEYS, *TWILIO_KEYS, *OPTIONAL_KEYS):
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def allowed_senders(config: dict[str, str]) -> set[str]:
    """Parse ALLOWED_SENDERS into a lowercase set of addresses and bare domains."""
    raw = config.get("ALLOWED_SENDERS", "")
    return {part.strip().lower().lstrip("@") for part in raw.split(",") if part.strip()}


def require(config: dict[str, str], keys: tuple[str, ...]) -> None:
    """Raise with a clear message listing every missing key at once."""
    missing = [k for k in keys if not config.get(k)]
    if missing:
        raise SystemExit(f"Missing required config: {', '.join(missing)} (see config.example.txt)")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def session() -> requests.Session:
    """One pooled session for the whole process: keep-alive plus retry on 429/5xx."""
    sess = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    sess.headers.update({"User-Agent": USER_AGENT})
    return sess


def get_json(url: str, **params: Any) -> Any:
    """GET and parse JSON, or None on any network/HTTP/parse error (never raises)."""
    try:
        resp = session().get(url, params=params or None, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("request failed: %s (%s)", url, exc)
        return None


def cache_answers(func: Callable[[str], str | None]) -> Callable[[str], str | None]:
    """Memoize only successful lookups, so a transient failure or an empty result
    isn't remembered as the permanent answer for that query."""
    store: dict[str, str] = {}

    @wraps(func)
    def wrapper(query: str) -> str | None:
        if query in store:
            return store[query]
        result = func(query)
        if result:
            if len(store) >= 512:  # bounded; SMS volume never gets close
                store.clear()
            store[query] = result
        return result

    return wrapper


def run_source(source: Callable[[str], str | None], arg: str) -> str | None:
    """Call a source, turning any unexpected error into None so a broken source
    falls back to a web search instead of crashing the reply."""
    try:
        return source(arg)
    except Exception as exc:  # a bad source must never take down the reply
        logger.warning("source %s failed: %s", getattr(source, "__name__", source), exc)
        return None


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
# Carrier gateways wrap the real text in boilerplate; strip the common ones.
CARRIER_NOISE = re.compile(
    r"Rogers MMS|This message is brought to you by|Rogers|Sent from my \w+",
    re.IGNORECASE,
)


def clean_query(text: str) -> str:
    """Drop carrier boilerplate and collapse whitespace to a single line."""
    text = CARRIER_NOISE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(html: str) -> str:
    """Flatten an HTML email body to a clean query string."""
    return clean_query(BeautifulSoup(html, "html.parser").get_text(" "))


def strip_refs(text: str) -> str:
    """Remove bracketed reference markers like [2] or [note] from prose."""
    return re.sub(r"\[[A-Za-z0-9]+\]", "", text).strip()


def truncate(text: str, limit: int) -> str:
    """Trim to ``limit`` chars on a word boundary, GSM-7 safe (plain '...').

    The result is never longer than ``limit``. Below four characters there is no room
    for the ellipsis, and ``text[:limit - 3]`` would slice from the end instead.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[: max(limit, 0)]
    cut = text[: limit - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "..."


# Words that carry no topic, so they never count as evidence that a result matches.
STOPWORDS = frozenset(
    """a an and are as at be been but by can did do does for from had has have how i if in
    is it its me my of on or that the their there these they this to was were what when
    where which who why will with you your""".split()  # noqa: SIM905 - reads better as prose
)


def content_words(text: str) -> set[str]:
    """Lowercase words of three or more letters that aren't stopwords."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def _shares_stem(a: str, b: str) -> bool:
    if a == b:
        return True
    common = len(os.path.commonprefix([a, b]))
    return common >= 4  # purify/purified, boil/boiler; not hike/hiking, open/opera


def looks_relevant(query: str, title: str) -> bool:
    """True when the result's title actually overlaps what was asked.

    Wikipedia's search returns a hit for any query built from real words, so without
    this the tool answers "how do I treat a blister" with a summary of a memoir that
    mentions blisters, and "is highway 4 open" with the wrong highway in the wrong
    province. On a trail with no data, a confident wrong answer is worse than none.
    """
    asked = content_words(query)
    return any(_shares_stem(t, q) for t in content_words(title) for q in asked)


# --------------------------------------------------------------------------- #
# Sources — each returns a short answer string, or None if it has nothing.
# Reference lookups (stable answers) are cached; time-sensitive ones are not.
# --------------------------------------------------------------------------- #
@cache_answers
def source_duckduckgo(query: str) -> str | None:
    """DuckDuckGo Instant Answer: direct answers, definitions, topic abstracts."""
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
    for topic in data.get("RelatedTopics", []):
        if isinstance(topic, dict) and topic.get("Text"):
            return str(topic["Text"]).strip()
    return None


@cache_answers
def source_wikipedia(query: str) -> str | None:
    """Top Wikipedia hit's lead summary, named and checked for relevance.

    The article title is part of the reply on purpose. It costs a few characters and
    it's the only way the reader can tell a real answer from a near miss.
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
        logger.info("dropped off-topic wikipedia hit %r for %r", title, query)
        return None

    summary = get_json(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
    )
    extract = (summary or {}).get("extract")
    if not extract:
        return None
    text = strip_refs(extract)
    # Most lead sentences open with the article name, so only prepend it when they
    # don't. Every duplicated character is one fewer character of actual answer.
    return text if text.lower().startswith(title.lower()) else f"{title}: {text}"


@cache_answers
def source_dictionary(word: str) -> str | None:
    """First one or two senses from the free Dictionary API."""
    entries = get_json(
        f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    )
    if not isinstance(entries, list) or not entries:
        return None
    senses = []
    for meaning in entries[0].get("meanings", [])[:2]:
        definitions = meaning.get("definitions", [])
        if definitions:
            pos = meaning.get("partOfSpeech", "")
            senses.append(f"({pos}) {definitions[0].get('definition', '')}".strip())
    return "; ".join(senses) or None


# WMO weather codes, grouped. Open-Meteo returns the number; this names it.
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
    """Name a geocoded place tightly enough to catch a wrong match: 'Tofino, BC, CA'."""
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
    """Current conditions from Open-Meteo, formatted plain for SMS (no emoji or degree sign)."""
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
    """Today's sunrise and sunset in the place's own timezone.

    The question you actually ask from a trailhead, and the one a general web search
    is worst at.
    """
    hit = geocode(place)
    if not hit:
        return None
    data = _forecast(hit, daily="sunrise,sunset")
    daily = (data or {}).get("daily") or {}
    try:
        sunrise, sunset = daily["sunrise"][0], daily["sunset"][0]
    except (KeyError, IndexError):
        return None
    return (
        f"{place_label(hit)}: sunrise {sunrise[11:16]}, sunset {sunset[11:16]} "
        f"({data.get('timezone_abbreviation', 'local')})"
    )


def source_stackoverflow(query: str) -> str | None:
    """Top Stack Overflow question plus its highest-voted answer body."""
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
    title = question.get("title", "")
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
    return f"{title} - {body}"


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
    "Commands: weather <place>, sun <place>, define <word>, wiki <topic>, so <query>, "
    "help. Anything else runs a web search."
)
NO_RESULT_HINT = (
    "No confident answer for '{target}'. Try a keyword instead of a question, "
    "or a command: weather, sun, define, wiki, so."
)


def default_search(query: str) -> str | None:
    """Query DuckDuckGo and Wikipedia at the same time; prefer DuckDuckGo."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        ddg = pool.submit(run_source, source_duckduckgo, query)
        wiki = pool.submit(run_source, source_wikipedia, query)
        return ddg.result() or wiki.result()


def answer(query: str, limit: int = DEFAULT_SMS_CHARS) -> str:
    """Route a query to a source (or a web search) and format it for one SMS reply.

    Never raises: a broken source falls back to a web search, and nothing convincing
    comes back as an honest miss rather than as the nearest unrelated article.
    """
    query = truncate(query.strip(), MAX_QUERY_CHARS)
    if not query:
        return "Empty message. Text 'help' for commands."

    command, _, rest = query.partition(" ")
    key = command.lower().strip(":,")
    if key in HELP_WORDS and not rest.strip():  # "help me ..." is a real query, not the command
        return truncate(HELP_TEXT, limit)

    provider = ROUTES.get(key)
    target, tag, result = query, "web", None
    if provider and rest.strip():
        target = rest.strip()
        result = run_source(provider, target)
        if result is not None:
            tag = key
    if result is None:  # no command, or the command's source came up empty
        result = default_search(target)
    if result is None:
        return truncate(NO_RESULT_HINT.format(target=target), limit)
    return truncate(f"{tag}: {result}", limit)


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #
def authenticate_gmail(config: dict[str, str]) -> Any:
    """Build a Gmail service, reusing the cached OAuth token when it's still valid."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = [config.get("GMAIL_SCOPE", DEFAULT_SCOPE)]
    token_path = Path(config["GMAIL_TOKEN_FILE"])
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_info(
            json.loads(token_path.read_text(encoding="utf-8")), scopes
        )
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config["GMAIL_CREDENTIALS_FILE"], scopes
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def get_label_id(service: Any, label_name: str) -> str | None:
    """Resolve a Gmail label's display name to its ID."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == label_name.lower():
            return str(label["id"])
    logger.error("Label '%s' not found", label_name)
    return None


def _find_body(part: dict[str, Any], mime: str) -> str | None:
    """Walk a (possibly nested multipart) payload for the first body of ``mime``."""
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        raw = base64.urlsafe_b64decode(part["body"]["data"])
        return raw.decode("utf-8", "replace")
    for sub in part.get("parts", []):
        found = _find_body(sub, mime)
        if found:
            return found
    return None


def extract_query(message: dict[str, Any]) -> str | None:
    """Pull the user's text out of a message, preferring plain text over HTML."""
    payload = message.get("payload", {})
    plain = _find_body(payload, "text/plain")
    if plain:
        return clean_query(plain)
    html = _find_body(payload, "text/html")
    return html_to_text(html) if html else None


def sender_address(message: dict[str, Any]) -> str:
    """The bare address out of the From header, lowercased. Empty if there isn't one."""
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == "from":
            match = re.search(r"[\w.+-]+@[\w.-]+", header.get("value", ""))
            return match.group(0).lower() if match else ""
    return ""


def sender_permitted(address: str, allowed: set[str]) -> bool:
    """Match a sender against the allowlist by full address or by domain.

    An empty allowlist permits everything, which is what an existing install gets on
    upgrade. main() warns about it at startup.
    """
    if not allowed:
        return True
    domain = address.rpartition("@")[2]
    return address in allowed or (bool(domain) and domain in allowed)


def unread_ids(service: Any, label_id: str) -> list[str]:
    """IDs of unread messages under the label, oldest first.

    Gmail caps a page at 100 and hands back a token for the rest. Without following
    it, a backlog over 100 stayed unread and then arrived all at once on a later poll.
    """
    ids: list[str] = []
    token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", labelIds=[label_id, "UNREAD"], pageToken=token)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            return list(reversed(ids))


def mark_read(service: Any, msg_id: str) -> None:
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


# --------------------------------------------------------------------------- #
# Twilio
# --------------------------------------------------------------------------- #
def make_sender(config: dict[str, str], dry_run: bool) -> Callable[[str], bool]:
    """Return a ``send(text) -> delivered`` function. Dry-run logs instead of texting.

    The Twilio client is built once and reused across the whole run.
    """
    if dry_run:

        def pretend(text: str) -> bool:
            logger.info("[dry-run] would send: %s", text)
            return True

        return pretend

    from twilio.rest import Client

    client = Client(config["TWILIO_ACCOUNT_SID"], config["TWILIO_AUTH_TOKEN"])
    to, from_ = config["PHONE_TO"], config["TWILIO_PHONE_FROM"]

    def send(text: str) -> bool:
        try:
            sms = client.messages.create(to=to, from_=from_, body=text)
            logger.info("sent %s", sms.sid)
            return True
        except Exception as exc:  # Twilio raises many subclasses; one text failing is not fatal
            logger.error("failed to send SMS: %s", exc)
            return False

    return send


# --------------------------------------------------------------------------- #
# Poll loop
# --------------------------------------------------------------------------- #
def process_once(
    service: Any,
    label_id: str,
    send: Callable[[str], bool],
    limit: int,
    *,
    allowed: set[str] | None = None,
    max_replies: int = DEFAULT_MAX_REPLIES,
) -> int:
    """Answer unread messages under the label and mark them read. Returns replies sent.

    At most ``max_replies`` go out per poll. Every reply is a billed SMS, so a flood
    of inbound mail must not turn into an unbounded flood of outbound texts. Anything
    over the cap stays unread and is picked up next time.
    """
    replied = 0
    for msg_id in unread_ids(service, label_id):
        if replied >= max_replies:
            logger.warning("hit the %d reply cap; the rest wait for the next poll", max_replies)
            break
        # Guard each message: one failure must not abort the batch or, worse, leave
        # a message unread so it's answered again on every future poll.
        try:
            message = service.users().messages().get(userId="me", id=msg_id).execute()
            sender = sender_address(message)
            query = extract_query(message)

            if not sender_permitted(sender, allowed or set()):
                logger.warning("ignoring message from %r (not in ALLOWED_SENDERS)", sender)
            elif query:
                logger.info("query from %s: %s", sender or "unknown", query)
                if send(answer(query, limit)):
                    replied += 1
            else:
                logger.warning("message %s had no readable text", msg_id)
            mark_read(service, msg_id)
        except Exception as exc:  # log and move on to the next message
            logger.error("failed on message %s: %s", msg_id, exc)
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
    """Poll forever. On startup, skip the existing backlog unless ``catch_up`` is set."""
    if not catch_up:
        for msg_id in unread_ids(service, label_id):
            mark_read(service, msg_id)
        send("Remote search online.")

    logger.info("monitoring label %s every %ds", label_id, interval)
    failures = 0
    while True:
        try:
            process_once(service, label_id, send, limit, allowed=allowed, max_replies=max_replies)
            failures = 0
        except Exception as exc:  # keep the loop alive across transient Gmail errors
            failures += 1
            logger.error("poll failed (%d in a row): %s", failures, exc)
        # Back off while Gmail is unhappy instead of hammering it every interval.
        sleep(min(interval * 2**failures, 300) if failures else interval)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Answer emailed questions over SMS.")
    parser.add_argument("--config", default="config.txt", help="path to the config file")
    parser.add_argument("--query", help="answer one query and exit (no Gmail/Twilio needed)")
    parser.add_argument("--once", action="store_true", help="process current unread mail and exit")
    parser.add_argument("--catch-up", action="store_true", help="answer the startup backlog too")
    parser.add_argument("--interval", type=int, help="seconds between polls")
    parser.add_argument("--max-chars", type=int, help="max SMS length")
    parser.add_argument("--max-replies", type=int, help="max texts sent per poll")
    parser.add_argument("--dry-run", action="store_true", help="log replies instead of texting")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.query:
        print(answer(args.query, args.max_chars or DEFAULT_SMS_CHARS))
        return

    config = load_config(args.config)
    require(config, GMAIL_REQUIRED + TWILIO_KEYS)
    limit = args.max_chars or int(config.get("MAX_SMS_CHARS", DEFAULT_SMS_CHARS))
    interval = args.interval or int(config.get("POLL_INTERVAL", 5))
    max_replies = args.max_replies or int(config.get("MAX_REPLIES_PER_POLL", DEFAULT_MAX_REPLIES))

    allowed = allowed_senders(config)
    if allowed:
        logger.info("answering only mail from: %s", ", ".join(sorted(allowed)))
    else:
        logger.warning(
            "ALLOWED_SENDERS is unset: anything that lands in the label gets answered, "
            "and every answer is a billed SMS. Set it to your carrier's gateway domain."
        )

    service = authenticate_gmail(config)
    label_id = get_label_id(service, config.get("LABEL_NAME", "Remote Server"))
    if not label_id:
        raise SystemExit(1)

    send = make_sender(config, args.dry_run)
    if args.once:
        count = process_once(
            service, label_id, send, limit, allowed=allowed, max_replies=max_replies
        )
        logger.info("sent %d repl%s", count, "y" if count == 1 else "ies")
    else:
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


if __name__ == "__main__":
    main()
