"""Web search through Brave when there's a key, then the engines the ddgs package scrapes."""

import logging
import time
from dataclasses import dataclass

from . import net
from .lookup import looks_relevant
from .net import SourceError
from .text import inline_text

logger = logging.getLogger("remotesearch")

# The ddgs backends, tried in this order. DuckDuckGo blocks automated searches after a
# burst, and the others scrape result pages that block or change now and then too.
# wikipedia and grokipedia are left out as they aren't web searches.
BACKENDS = ("duckduckgo", "brave", "mojeek", "yahoo", "google", "startpage")
# Seconds to wait before each retry once none of them answers. A backend that failed
# is left alone for 2 minutes after that, instead of being asked again (and blocked
# for longer) on every text in between.
RETRY_WAITS = (2, 5)
COOLDOWN = 120
_resting: dict[str, float] = {}  # backend name to the time.monotonic() it can be asked again

# Reddit's API is closed to personal apps and Quora has none, so these come from
# searching the site. The answer is the thread title and the search snippet.
SITES = {
    "reddit": "reddit.com",
    "quora": "quora.com",
    "youtube": "youtube.com",
}


@dataclass(frozen=True)
class Hit:
    title: str
    snippet: str
    url: str


def ddgs_search(query: str) -> list[Hit]:
    """Top five results from the first ddgs backend that answers."""
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    down = SourceError("the web search", "it's limiting requests")
    ready = [b for b in BACKENDS if _resting.get(b, 0) <= time.monotonic()]
    if not ready:
        raise down
    for wait in (0, *RETRY_WAITS):
        time.sleep(wait)
        for n, backend in enumerate(ready):
            try:
                rows = DDGS(timeout=net.REQUEST_TIMEOUT).text(
                    query, region="ca-en", max_results=5, backend=backend
                )
            except DDGSException as exc:
                # ddgs reports a block as "No results found", since a blocked request
                # gets an empty page. A real search that finds nothing at all is rare
                # enough that it's treated the same way.
                logger.warning("The %s search failed (%s)", backend, exc)
                continue
            _resting.update(dict.fromkeys(ready[:n], time.monotonic() + COOLDOWN))
            return [
                Hit(str(r.get("title", "")), str(r.get("body", "")), str(r.get("href", "")))
                for r in rows
            ]
    _resting.update(dict.fromkeys(ready, time.monotonic() + COOLDOWN))
    raise down


def brave(query: str, key: str) -> list[Hit]:
    """Top five results from the Brave Search API."""
    data = net.get_json(
        "https://api.search.brave.com/res/v1/web/search",
        "Brave Search",
        params={"q": query, "count": 5},
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )
    results = ((data or {}).get("web") or {}).get("results") or []
    # Brave marks the matched words with <strong>, and those tags sit mid-sentence.
    return [
        Hit(inline_text(r.get("title", "")), inline_text(r.get("description", "")), r["url"])
        for r in results
        if isinstance(r, dict) and r.get("url")
    ]


def web_results(query: str, brave_key: str = "") -> list[Hit]:
    """Brave's API when there's a key, and the ddgs backends without one or when it fails."""
    if brave_key:
        try:
            return brave(query, brave_key)
        except SourceError as exc:
            logger.warning("Brave Search failed (%s), so trying the other engines", exc.why)
    return ddgs_search(query)


def site_query(site: str, query: str) -> str:
    return f"site:{site} {query}"


def best_hit(query: str, hits: list[Hit]) -> Hit | None:
    """The first hit whose title or snippet overlaps what was asked."""
    return next((h for h in hits if looks_relevant(query, f"{h.title} {h.snippet}")), None)


def snippet_answer(query: str, hits: list[Hit]) -> str | None:
    hit = best_hit(query, hits)
    if not hit:
        return None
    title = hit.title.rstrip(" .")
    return f"{title}: {hit.snippet}" if hit.snippet else title
