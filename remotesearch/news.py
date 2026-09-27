"""Headlines from Google News and scores from ESPN's public scoreboards."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from . import net
from .net import SourceError

logger = logging.getLogger("remotesearch")

GOOGLE_NEWS = "https://news.google.com/rss"
DEFAULT_NEWS_REGION = "CA"
MAX_HEADLINES = 8


def news_edition(region: str) -> dict[str, str]:
    """Google News's parameters for NEWS_REGION, a country code like GB, or CA:fr for French."""
    country, _, language = region.partition(":")
    country, language = country.strip().upper(), language.strip().lower() or "en"
    if not re.fullmatch(r"[A-Z]{2}", country) or not re.fullmatch(r"[a-z]{2,3}", language):
        raise SystemExit(
            f"NEWS_REGION is {region!r}, which isn't a country code like CA, or CA:fr for "
            "the news in French."
        )
    return {"hl": f"{language}-{country}", "gl": country, "ceid": f"{country}:{language}"}


def source_news(topic: str, edition: dict[str, str]) -> str | None:
    """Top headlines from Google News's ``edition``, or the latest there on ``topic``."""
    url, params = GOOGLE_NEWS, edition
    if topic:
        url, params = f"{GOOGLE_NEWS}/search", {"q": topic, **edition}
    feed = net.get_text(url, "Google News", params=params)
    if not feed:
        return None
    try:
        # ElementTree never fetches external entities, and the expat under Python 3.11
        # and later caps entity expansion, so a hostile feed can't do much here.
        root = ElementTree.fromstring(feed)  # noqa: S314
    except ElementTree.ParseError:
        raise SourceError("Google News", "its feed wasn't readable") from None
    titles = [t.strip() for t in (i.findtext("title") for i in root.iter("item")) if t]
    if not titles:
        return None
    return " ".join(f"{n}) {t}." for n, t in enumerate(titles[:MAX_HEADLINES], 1))


# ESPN's own names, looked through in this order when a text names only a team.
LEAGUES = (
    ("hockey", "nhl"),
    ("basketball", "nba"),
    ("football", "nfl"),
    ("baseball", "mlb"),
    ("soccer", "usa.1"),
    ("basketball", "wnba"),
    ("soccer", "eng.1"),
)
TEAM_FIELDS = ("displayName", "shortDisplayName", "location", "name")


def scoreboard(sport: str, league: str, day: str = "") -> list[dict[str, Any]]:
    data = net.get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard",
        "ESPN",
        params={"dates": day} if day else None,
    )
    return [e for e in (data or {}).get("events") or [] if isinstance(e, dict)]


def plays_in(team: str, event: dict[str, Any]) -> bool:
    for side in (event.get("competitions") or [{}])[0].get("competitors") or []:
        info = side.get("team") or {}
        if team == str(info.get("abbreviation", "")).lower():
            return True
        if len(team) >= 3 and any(team in str(info.get(f, "")).lower() for f in TEAM_FIELDS):
            return True
    return False


def game_line(event: dict[str, Any]) -> str:
    """One game as "NYM 7 @ WSH 1, Final" or "VAN @ EDM, 9/29 - 10:00 PM EDT"."""
    status = (event.get("status") or {}).get("type") or {}
    sides = (event.get("competitions") or [{}])[0].get("competitors") or []
    away: dict[str, Any] = next((s for s in sides if s.get("homeAway") == "away"), {})
    home: dict[str, Any] = next((s for s in sides if s.get("homeAway") == "home"), {})

    def name(side: dict[str, Any]) -> str:
        abbr = (side.get("team") or {}).get("abbreviation", "?")
        return abbr if status.get("state") == "pre" else f"{abbr} {side.get('score', '?')}"

    return f"{name(away)} @ {name(home)}, {status.get('shortDetail', '')}".rstrip(", ")


def source_scores(team: str) -> str | None:
    """Live, final or upcoming games for a team, found by name across the big leagues."""
    team = team.lower().strip()
    # ESPN's scoreboard dates run on US Eastern time.
    today = datetime.now(ZoneInfo("America/New_York"))
    yesterday = (today - timedelta(days=1)).strftime("%Y%m%d")
    boards = [(s, lg, d) for s, lg in LEAGUES for d in ("", yesterday)]
    failures: list[SourceError] = []

    def fetch(board: tuple[str, str, str]) -> list[dict[str, Any]]:
        try:
            return scoreboard(*board)
        except SourceError as exc:
            failures.append(exc)
            return []

    with ThreadPoolExecutor(max_workers=7) as pool:
        found = [e for events in pool.map(fetch, boards) for e in events if plays_in(team, e)]
    if not found and failures:
        raise failures[0]
    games: dict[str, dict[str, Any]] = {}
    for event in found:
        games.setdefault(str(event.get("id")), event)

    def state(event: dict[str, Any]) -> str:
        return str(((event.get("status") or {}).get("type") or {}).get("state", ""))

    def when(event: dict[str, Any]) -> str:
        return str(event.get("date", ""))

    # A game on now comes first, then finals newest first, then what's coming up.
    live = [e for e in games.values() if state(e) == "in"]
    finals = sorted((e for e in games.values() if state(e) == "post"), key=when, reverse=True)
    later = sorted((e for e in games.values() if state(e) not in ("in", "post")), key=when)
    return " / ".join(game_line(e) for e in (live + finals + later)[:3]) or None
