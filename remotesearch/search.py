"""Web search through DuckDuckGo, or Brave when there's a key, and the site commands."""

import logging
import re
import time
from dataclasses import dataclass

from bs4 import BeautifulSoup

from . import net
from .lookup import looks_relevant
from .net import SourceError

logger = logging.getLogger("remotesearch")

# Seconds to wait before each retry once DuckDuckGo stops answering. If it still
# won't answer, it's left alone for 2 minutes instead of being asked again (and
# blocked for longer) on every text in between.
DDG_RETRY_WAITS = (2, 5)
DDG_COOLDOWN = 120
_ddg_blocked_until = 0.0

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


def duckduckgo(query: str) -> list[Hit]:
    """Top five results from DuckDuckGo through the ddgs package."""
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    global _ddg_blocked_until
    if time.monotonic() < _ddg_blocked_until:
        raise SourceError("DuckDuckGo", "it's limiting requests")
    for wait in (0, *DDG_RETRY_WAITS):
        time.sleep(wait)
        try:
            rows = DDGS(timeout=net.REQUEST_TIMEOUT).text(
                query, region="ca-en", max_results=5, backend="duckduckgo"
            )
        except DDGSException as exc:
            # ddgs reports a block as "No results found", since DuckDuckGo answers a
            # blocked request with an empty page. A real search that finds nothing at
            # all is rare enough that it's treated the same way.
            logger.warning("DuckDuckGo search failed (%s)", exc)
            continue
        return [
            Hit(str(r.get("title", "")), str(r.get("body", "")), str(r.get("href", "")))
            for r in rows
        ]
    _ddg_blocked_until = time.monotonic() + DDG_COOLDOWN
    raise SourceError("DuckDuckGo", "it's limiting requests")


def brave(query: str, key: str) -> list[Hit]:
    """Top five results from the Brave Search API."""
    data = net.get_json(
        "https://api.search.brave.com/res/v1/web/search",
        "Brave Search",
        params={"q": query, "count": 5},
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )
    results = ((data or {}).get("web") or {}).get("results") or []
    return [
        Hit(_inline(r.get("title", "")), _inline(r.get("description", "")), r["url"])
        for r in results
        if isinstance(r, dict) and r.get("url")
    ]


def _inline(markup: str) -> str:
    """Brave marks the matched words with <strong>, and those tags sit mid-sentence."""
    return re.sub(r"\s+", " ", BeautifulSoup(markup, "html.parser").get_text()).strip()


def web_results(query: str, brave_key: str = "") -> list[Hit]:
    return brave(query, brave_key) if brave_key else duckduckgo(query)


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
