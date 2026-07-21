"""Offline checks for the pure helpers. Run: python test_remotesearch.py

Network sources aren't covered here (they need live APIs); exercise those with
`python RemoteSearch.py --query "..."`.
"""

from RemoteSearch import (
    HELP_TEXT,
    answer,
    cache_answers,
    clean_query,
    html_to_text,
    run_source,
    strip_refs,
    truncate,
)


def test_clean_query() -> None:
    assert clean_query("Rogers MMS  what is\n\nphotosynthesis") == "what is photosynthesis"
    assert clean_query("  spaced   out  ") == "spaced out"


def test_html_to_text() -> None:
    assert html_to_text("<p>hello <b>world</b></p>") == "hello world"


def test_strip_refs() -> None:
    assert strip_refs("Water[1] is wet[note].") == "Water is wet."


def test_truncate() -> None:
    assert truncate("short", 300) == "short"
    assert truncate("one two three four", 12) == "one two..."
    assert len(truncate("x" * 500, 300)) == 300


def test_answer_help() -> None:
    for word in ("help", "HELP", "?"):
        assert answer(word) == HELP_TEXT
    assert answer("") == "Empty message. Text 'help' for commands."


def test_run_source_swallows_errors() -> None:
    def boom(_: str) -> str | None:
        raise RuntimeError("network exploded")

    assert run_source(boom, "x") is None
    assert run_source(lambda q: q.upper(), "hi") == "HI"


def test_cache_answers_caches_only_success() -> None:
    calls = {"n": 0}

    @cache_answers
    def flaky(query: str) -> str | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else f"ok:{query}"

    assert flaky("a") is None  # first call fails, must not be cached
    assert flaky("a") == "ok:a"  # retried, now succeeds
    assert flaky("a") == "ok:a"  # served from cache
    assert calls["n"] == 2


if __name__ == "__main__":
    for _name, _case in sorted(globals().items()):
        if _name.startswith("test_"):
            _case()
            print(f"ok  {_name}")
    print("all passed")
