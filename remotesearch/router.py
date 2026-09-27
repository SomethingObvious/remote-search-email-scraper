"""The channel-agnostic core: a question comes in from a sender and a short answer goes out."""

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from . import ai
from .lookup import (
    STOPWORDS,
    source_dictionary,
    source_duckduckgo,
    source_stackoverflow,
    source_wikipedia,
    words,
)
from .net import SourceError
from .news import DEFAULT_NEWS_REGION, news_edition, source_news, source_scores
from .outdoors import (
    source_avalanche,
    source_drive,
    source_forecast,
    source_roads,
    source_sun,
    source_tides,
    source_weather,
)
from .places import place_words, source_business, source_time
from .search import SITES, site_query, snippet_answer, web_results
from .state import State
from .text import MAX_QUERY_CHARS, paginate, sms_text, truncate
from .tools import mymemory, source_calc, source_convert, translation_request

logger = logging.getLogger("remotesearch")

DEFAULT_SMS_CHARS = 300  # two GSM-7 segments hold 306
EMAIL_MAX_CHARS = 4000  # an email has room for the whole answer, so it isn't paged

HELP_WORDS = {"help", "?", "commands"}
HELP_TEXT = (
    "Ask anything, or start with weather forecast sun tide avy road drive calc convert time "
    "translate news score hours define wiki reddit site. help <word> for more"
)
EMPTY_TEXT = "Your text came through empty. Text help to see the commands."
NO_RESULT_TEXT = (
    "Couldn't find a good answer for '{target}'. Try a keyword instead of a whole "
    "question, or text help for the commands."
)
DOWN_TEXT = "{name} isn't answering right now ({why}). Try again in a few minutes."
NOTHING_MORE_TEXT = "There's nothing more to send. Text a new question, or help for the commands."
ONLINE_TEXT = "Remote search is running. Text help to see the commands."
TRANSLATE_USAGE = (
    "Text it as translate <words> to <language>, like 'translate where is the bus to french'."
)
DRIVE_USAGE = "Text it as drive <place> to <place>, like 'drive Vancouver to Whistler'."


@dataclass(frozen=True)
class Command:
    words: tuple[str, ...]  # the first one tags the reply
    run: Callable[[str], str | None]
    help: str
    # A word that also starts ordinary sentences, like "so" or "time", only counts as
    # a command when the next word isn't a stopword. "so what is lye" is a question.
    loose: bool = False
    bare: bool = False  # it works with nothing after it, like "news"


@dataclass(frozen=True)
class Incoming:
    """One question from one sender, and the way its answer goes back to them."""

    sender: str
    text: str
    channel: str  # "sms" or "email", which decides how the answer is cut to size
    send: Callable[[str], bool]


def down_text(exc: SourceError) -> str:
    return DOWN_TEXT.format(name=exc.name[:1].upper() + exc.name[1:], why=exc.why)


def attempt(source: Callable[[str], Any], arg: str) -> tuple[Any, SourceError | None]:
    """Call a source, returning its result and any outage instead of raising."""
    try:
        return source(arg), None
    except SourceError as exc:
        logger.warning("%s is down: %s", exc.name, exc.why)
        return None, exc
    except Exception as exc:  # a bug in one source still leaves the others to answer
        logger.warning("%s failed on %r: %s", getattr(source, "__name__", source), arg, exc)
        return None, None


class Answerer:
    """Turns one question into one answer, with no idea who asked or how it'll be sent."""

    def __init__(self, config: dict[str, str] | None = None, limit: int = DEFAULT_SMS_CHARS):
        config = config or {}
        self.brave_key = config.get("BRAVE_API_KEY", "")
        self.news_edition = news_edition(config.get("NEWS_REGION") or DEFAULT_NEWS_REGION)
        self.model = ai.model_from_config(config)
        self.limit = limit
        self.commands = self._commands()

    def _commands(self) -> dict[str, Command]:
        def site(name: str) -> Callable[[str], str | None]:
            return lambda q: self.site_search(SITES[name], q)

        table = [
            Command(
                ("weather",),
                source_weather,
                "weather <place>: current conditions and any weather alerts, like 'weather "
                "Tofino'. A region picks the right town, like 'weather Paris, France'.",
                loose=True,
            ),
            Command(
                ("forecast",),
                source_forecast,
                "forecast <place>: three days of highs, lows, rain and wind from Open-Meteo, "
                "plus any Environment Canada alerts.",
                loose=True,
            ),
            Command(
                ("sun", "sunrise", "sunset"),
                source_sun,
                "sun <place>: today's sunrise and sunset in that place's time zone.",
                loose=True,
            ),
            Command(
                ("tide", "tides"),
                source_tides,
                "tide <place>: the next highs and lows at the nearest Canadian Hydrographic "
                "Service station, like 'tide Tofino'.",
                loose=True,
            ),
            Command(
                ("avy", "avalanche"),
                source_avalanche,
                "avy <place>: today's danger ratings and problems from Avalanche Canada's "
                "forecast for that spot, like 'avy Whistler'.",
            ),
            Command(
                ("road", "roads"),
                source_roads,
                "road <highway or BC town>: DriveBC closures and events, worst first, like "
                "'road hwy 99', 'road coquihalla' or 'road Squamish'.",
                loose=True,
            ),
            Command(
                ("drive",),
                self.drive,
                "drive <place> to <place>: driving distance and time from OSRM, which knows "
                "nothing about traffic or closures.",
                loose=True,
            ),
            Command(
                ("calc", "calculate"),
                source_calc,
                "calc <sum>: arithmetic, like 'calc 15% of 80' or 'calc (3+4)^2'. It also "
                "knows sqrt, sin, cos, tan, log, ln and pi.",
            ),
            Command(
                ("convert",),
                source_convert,
                "convert <amount> <unit> to <unit>: length, weight, volume, speed, "
                "temperature or currency, like 'convert 10 km to mi' or 'convert 50 usd to cad'.",
                loose=True,
            ),
            Command(
                ("time",),
                source_time,
                "time <place>: the local time there, like 'time Tokyo'.",
                loose=True,
            ),
            Command(
                ("translate",),
                self.translate,
                "translate <words> to <language>, like 'translate where is the bus to french'.",
            ),
            Command(
                ("news",),
                lambda topic: source_news(topic, self.news_edition),
                "news: the top headlines from Google News. news <topic>: the latest on that topic.",
                loose=True,
                bare=True,
            ),
            Command(
                ("score", "scores"),
                source_scores,
                "score <team>: a live, final or upcoming NHL, NBA, NFL, MLB, MLS, WNBA or "
                "Premier League game from ESPN, like 'score canucks'.",
                loose=True,
            ),
            Command(
                ("hours", "phone", "address"),
                source_business,
                "hours <business> <town>: opening hours, address and phone number from "
                "OpenStreetMap, like 'hours Tim Hortons Hope'.",
                loose=True,
            ),
            Command(
                ("define", "def", "dict"),
                source_dictionary,
                "define <word>: the first meanings from Wiktionary.",
            ),
            Command(
                ("wiki",),
                source_wikipedia,
                "wiki <topic>: the Wikipedia summary, named so a near miss is easy to spot.",
            ),
            Command(
                ("so", "stack", "stackoverflow"),
                source_stackoverflow,
                "so <question>: the top Stack Overflow question and its best answer.",
                loose=True,
            ),
            Command(
                ("reddit",),
                site("reddit"),
                "reddit <question>: the best matching thread from a web search of reddit.com. "
                "quora and youtube work the same way.",
            ),
            Command(
                ("quora",),
                site("quora"),
                "quora <question>: the best matching answer from a web search of quora.com.",
            ),
            Command(
                ("youtube",),
                site("youtube"),
                "youtube <question>: the best matching video from a web search of youtube.com.",
            ),
            Command(
                ("site",),
                self.site_command,
                "site <domain> <question>: a web search of one site, like 'site mec.ca "
                "return policy'.",
                loose=True,
            ),
        ]
        return {word: command for command in table for word in command.words}

    def answer(self, query: str) -> str:
        """The whole answer to one text, before it's cut to fit an SMS."""
        query = re.sub(r"\s+", " ", query).strip()[:MAX_QUERY_CHARS].strip()
        if not query:
            return EMPTY_TEXT
        first, _, rest = query.partition(" ")
        key, rest = first.lower().strip(":,"), rest.strip()
        if key in HELP_WORDS:
            if not rest:
                return HELP_TEXT
            asked = self.commands.get(rest.lower().strip(":,?"))
            if asked:
                return asked.help
            # "help me ..." is a real question, so it goes on to the search.

        command = self.commands.get(key)
        if not command or not (rest or command.bare) or self._reads_as_prose(command, rest):
            return self.web(query)
        result, down = attempt(command.run, rest)
        if down:
            return down_text(down)
        if result:
            return f"{command.words[0]}: {result}"
        return self.web(rest or query, skip=command.run)

    @staticmethod
    def _reads_as_prose(command: Command, rest: str) -> bool:
        following = words(place_words(rest))[:1]
        return command.loose and bool(following) and following[0] in STOPWORDS

    def web(self, query: str, skip: Callable[[str], Any] | None = None) -> str:
        """Ask the instant answers, Wikipedia and a web search at once, and pick the best."""
        lookups = [s for s in (source_duckduckgo, source_wikipedia) if s != skip]
        sources = [*lookups, self.search]
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            results = list(pool.map(lambda s: attempt(s, query), sources))
        # DuckDuckGo's instant answer beats Wikipedia, and both beat a search snippet.
        reference = [found for found, _ in results[:-1] if found]
        hits = results[-1][0] or []
        downs = [down for _, down in results if down]

        if self.model and (reference or hits):
            written = ai.answer_from_results(self.model, query, hits, reference, self.limit)
            if written:
                return f"ai: {written}"
        found = reference[0] if reference else snippet_answer(query, hits)
        if found:
            return f"web: {found}"
        if downs:
            return down_text(downs[0])
        # The question is shortened so the advice after it still fits in the reply.
        return NO_RESULT_TEXT.format(target=truncate(query, 60))

    def search(self, query: str) -> list[Any]:
        return web_results(query, self.brave_key)

    def site_search(self, site: str, query: str) -> str | None:
        hits = web_results(site_query(site, query), self.brave_key)
        if self.model and hits:
            written = ai.answer_from_results(self.model, query, hits, [], self.limit)
            if written:
                return written
        return snippet_answer(query, hits)

    def site_command(self, text: str) -> str | None:
        domain, _, query = text.partition(" ")
        if "." not in domain or not query.strip():
            return None
        return self.site_search(domain.lower().split("//")[-1].strip("/"), query)

    def translate(self, text: str) -> str | None:
        request = translation_request(text)
        if not request:
            return TRANSLATE_USAGE
        words_, language, code = request
        result = ai.translate(self.model, words_, language) if self.model else None
        result = result or mymemory(words_, code)
        return f"{language}: {result}" if result else None

    @staticmethod
    def drive(text: str) -> str | None:
        return source_drive(text) if " to " in text else DRIVE_USAGE


class Responder:
    """Answers each sender, and keeps the rest of a long SMS answer for when they text "more"."""

    def __init__(self, answerer: Answerer, state: State | None, limit: int) -> None:
        self.answerer = answerer
        self.state = state
        self.limit = limit

    def reply(self, sender: str, text: str, channel: str = "sms") -> str:
        if text.strip().lower().rstrip(".!?") == "more":
            page = self.state.next_page(sender) if self.state else None
            return page or NOTHING_MORE_TEXT
        full = self.answerer.answer(text)
        if channel == "email":
            return truncate(full, EMAIL_MAX_CHARS)
        pages = paginate(sms_text(full), self.limit)
        if self.state:
            self.state.set_pages(sender, pages[1:])
        return pages[0]


def answer(query: str, limit: int = DEFAULT_SMS_CHARS, config: dict[str, str] | None = None) -> str:
    """The first SMS of the answer to ``query``, which is what --query prints."""
    return Responder(Answerer(config, limit), None, limit).reply("", query)
