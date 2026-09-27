"""Reference lookups: DuckDuckGo's instant answers, Wikipedia, a dictionary and Stack Overflow."""

import html
import logging
import os
import re
import unicodedata
import urllib.parse

from bs4 import BeautifulSoup

from . import net
from .text import strip_refs

logger = logging.getLogger("remotesearch")

# Words that carry no topic, so they never count as evidence that a result matches.
STOPWORDS = frozenset(
    """a an and are as at be been but by can did do does for from had has have how i if in
    is it its me my of on or that the their there these they this to was were what when
    where which who why will with you your""".split()  # noqa: SIM905 - reads better as prose
)


def words(text: str) -> list[str]:
    """Lowercase words with their accents dropped, so "Forêt" matches "foret"."""
    plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[a-z0-9]+", plain)


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
    data = net.get_json(
        "https://api.duckduckgo.com/",
        "DuckDuckGo",
        params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
    )
    if not isinstance(data, dict):
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
    hits = net.get_json(
        "https://en.wikipedia.org/w/api.php",
        "Wikipedia",
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": "1",
            "format": "json",
        },
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
    summary = net.get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{path}", "Wikipedia")
    if not summary or summary.get("type") == "disambiguation" or not summary.get("extract"):
        return None
    text = strip_refs(summary["extract"])
    # Most lead sentences open with the article name, so it's only added when they
    # don't. Every repeated character is one fewer character of answer.
    return text if text.lower().startswith(title.lower()) else f"{title}: {text}"


def source_dictionary(word: str) -> str | None:
    """First one or two senses from the free Dictionary API."""
    entries = net.get_json(
        f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word, safe='')}",
        "the dictionary",
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


def source_stackoverflow(query: str) -> str | None:
    """Top Stack Overflow question plus its highest-voted answer."""
    found = net.get_json(
        "https://api.stackexchange.com/2.3/search/advanced",
        "Stack Overflow",
        params={
            "order": "desc",
            "sort": "relevance",
            "q": query,
            "site": "stackoverflow",
            "pagesize": "1",
        },
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
    answers = net.get_json(
        f"https://api.stackexchange.com/2.3/questions/{question_id}/answers",
        "Stack Overflow",
        params={
            "order": "desc",
            "sort": "votes",
            "site": "stackoverflow",
            "pagesize": "1",
            "filter": "withbody",
        },
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
