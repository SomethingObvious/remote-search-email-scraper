"""Cleaning up what comes in, and fitting what goes out into SMS."""

import re
import unicodedata

from bs4 import BeautifulSoup

MAX_QUERY_CHARS = 200  # anything longer is a signature or boilerplate, not a question
MORE = " (more)"  # ends every page but the last, and HELP says to text "more"

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


GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")

# One character outside GSM-7 makes Twilio send the whole reply as UCS-2, where a
# segment holds 67 characters instead of 153, so a 300-character answer bills as 5.
# The brackets and the rest of GSM-7's extension table cost two characters each, so
# they're swapped too, which keeps a 300-character reply at two segments.
SMS_PUNCTUATION = str.maketrans(
    {
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{MINUS SIGN}": "-",
        "`": "'",
        "[": "(",
        "]": ")",
        "{": "(",
        "}": ")",
        "~": "-",
        "|": "/",
        "\\": "/",
        "\r": "",
    }
)


def sms_text(text: str) -> str:
    """Fold text into GSM-7, keeping the accents it has (like é) and dropping the rest."""
    out = []
    for c in text.translate(SMS_PUNCTUATION):
        if c in GSM7_BASIC:
            out.append(c)
        elif unicodedata.category(c) == "Pd":  # every dash Unicode knows of
            out.append("-")
        else:
            out.append(unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode("ascii"))
    return "".join(out)


def sms_segments(text: str) -> int:
    """How many SMS a carrier bills ``text`` as, going by its encoding and length."""
    if all(c in GSM7_BASIC or c in GSM7_EXTENSION for c in text):
        units = len(text) + sum(c in GSM7_EXTENSION for c in text)
        single, multi = 160, 153
    else:
        # UCS-2 counts UTF-16 code units, so an emoji takes two.
        units = len(text.encode("utf-16-le")) // 2
        single, multi = 70, 67
    return 1 if units <= single else -(-units // multi)


def paginate(text: str, limit: int, most: int = 6) -> list[str]:
    """Split ``text`` into pages of at most ``limit`` characters, each but the last ending in MORE.

    Past ``most`` pages the last one is cut short with '...', since nobody pages
    through a novel by SMS.
    """
    text = re.sub(r"\s+", " ", text).strip()
    room = limit - len(MORE)
    if room < 20:  # too small to page, so it's just cut
        return [truncate(text, limit)]
    pages: list[str] = []
    while len(text) > limit and len(pages) < most - 1:
        cut = text[:room]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        pages.append(cut.rstrip() + MORE)
        text = text[len(cut) :].lstrip()
    pages.append(truncate(text, limit))
    return pages
