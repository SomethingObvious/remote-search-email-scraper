import unicodedata

from remotesearch.text import (
    MORE,
    clean_query,
    html_to_text,
    paginate,
    sms_segments,
    sms_text,
    strip_refs,
    truncate,
)


def test_clean_query() -> None:
    assert clean_query("Rogers MMS  what is\n\nphotosynthesis") == "what is photosynthesis"
    assert clean_query("  spaced   out  ") == "spaced out"
    assert clean_query("who is Fred Rogers") == "who is Fred Rogers"
    assert clean_query("Rogerson family") == "Rogerson family"
    assert clean_query("sun Tofino\nRogers\n") == "sun Tofino"
    assert clean_query("define albedo\nSent from my iPhone") == "define albedo"


def test_html_to_text() -> None:
    assert html_to_text("<p>hello <b>world</b></p>") == "hello world"


def test_strip_refs() -> None:
    assert strip_refs("Water[1] is wet[note].") == "Water is wet."


def test_truncate() -> None:
    assert truncate("short", 300) == "short"
    assert truncate("one two three four", 12) == "one two..."
    assert len(truncate("x" * 500, 300)) == 300
    # Below 4 characters there's no room for the dots, and it must still fit the limit.
    for limit in range(8):
        assert len(truncate("abcdef ghijkl", limit)) <= limit, limit


def test_sms_text_keeps_gsm7_accents() -> None:
    assert sms_text("Montréal, Québec") == "Montréal, Québec"  # é is in GSM-7
    # ô isn't in GSM-7, so it loses its accent, and the curly apostrophe goes straight.
    assert sms_text("Où est l\N{RIGHT SINGLE QUOTATION MARK}hôpital") == "Où est l'hopital"
    assert sms_text("\N{LEFT DOUBLE QUOTATION MARK}hi\N{RIGHT DOUBLE QUOTATION MARK}") == '"hi"'
    for name in ("HYPHEN", "EN DASH", "EM DASH", "HORIZONTAL BAR", "MINUS SIGN"):
        assert sms_text(unicodedata.lookup(name)) == "-", name
    assert sms_text("wait\N{HORIZONTAL ELLIPSIS}") == "wait..."
    assert sms_text("15°C") == "15C"
    assert sms_text("smile \N{GRINNING FACE}") == "smile "
    assert sms_text("plain text") == "plain text"


def test_sms_text_swaps_the_extension_table() -> None:
    assert sms_text("[1] {x} ~ a|b c\\d") == "(1) (x) - a/b c/d"


def test_sms_segments_counts_gsm7_and_ucs2() -> None:
    assert sms_segments("a" * 160) == 1
    assert sms_segments("a" * 161) == 2
    assert sms_segments("a" * 306) == 2
    assert sms_segments("a" * 307) == 3
    assert sms_segments("é" * 160) == 1  # still GSM-7
    assert sms_segments("^" * 80) == 1  # extension characters count twice
    assert sms_segments("^" * 81) == 2
    assert sms_segments("ê" * 70) == 1  # one character outside GSM-7 means UCS-2
    assert sms_segments("ê" * 71) == 2
    assert sms_segments("\N{GRINNING FACE}" * 35) == 1  # an emoji is two UTF-16 units
    assert sms_segments("\N{GRINNING FACE}" * 36) == 2


def test_folded_replies_of_300_characters_fit_two_segments() -> None:
    awkward = (
        "\N{LEFT DOUBLE QUOTATION MARK}Québec\N{RIGHT DOUBLE QUOTATION MARK} \N{EN DASH} naïve "
        "café ½ ~ [ref] {x} | ê ô ç Straße \N{HORIZONTAL ELLIPSIS} °C \N{GRINNING FACE} "
    )
    text = sms_text(awkward * 20)[:300]
    assert all(c.isascii() or c in "éèùìòÇØøÅåÆæßÉÄÖÑÜäöñüà§¿¡£¥¤" for c in text)
    assert sms_segments(text) <= 2


def test_paginate_splits_on_words() -> None:
    text = " ".join(f"word{i}" for i in range(200))
    pages = paginate(text, 300)
    assert len(pages) == 6
    assert all(len(p) <= 300 for p in pages)
    assert all(p.endswith(MORE) for p in pages[:-1])
    assert not pages[-1].endswith(MORE)
    # Nothing is lost or repeated where one page ends and the next begins.
    assert " ".join(p.removesuffix(MORE) for p in pages) == text
    assert pages[0].endswith("word41 word42 (more)")
    assert pages[1].startswith("word43 ")


def test_paginate_cuts_short_after_the_last_page() -> None:
    pages = paginate("x " * 2000, 100, most=3)
    assert len(pages) == 3
    assert pages[-1].endswith("...")
    assert all(len(p) <= 100 for p in pages)


def test_paginate_short_text_and_tiny_limits() -> None:
    assert paginate("hello  there", 300) == ["hello there"]
    assert paginate("one two three four five six", 10) == ["one..."]
