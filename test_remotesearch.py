"""Offline checks for the pure helpers. Run: python test_remotesearch.py

No network here. The live sources are exercised with
`python RemoteSearch.py --query "..."`.
"""

import base64

from RemoteSearch import (
    DEFAULT_MAX_REPLIES,
    HELP_TEXT,
    MAX_QUERY_CHARS,
    allowed_senders,
    answer,
    cache_answers,
    clean_query,
    content_words,
    extract_query,
    html_to_text,
    looks_relevant,
    place_label,
    run_source,
    sender_address,
    sender_permitted,
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
    # The result must never exceed the limit. Below 4 chars there is no room for the
    # ellipsis, and the old slice ran off the end and returned more than it was given.
    for limit in range(8):
        assert len(truncate("abcdef ghijkl", limit)) <= limit, limit


def test_answer_help() -> None:
    for word in ("help", "HELP", "?"):
        assert answer(word) == HELP_TEXT
    assert answer("") == "Empty message. Text 'help' for commands."
    assert answer("   ") == "Empty message. Text 'help' for commands."


def test_answer_caps_query_length() -> None:
    # A long mail body must not become a giant URL parameter. No network is touched
    # because the source is never reached: this only checks the reply is bounded.
    assert len(answer("help " + "x" * 5000, limit=300)) <= 300


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


# --- relevance ---------------------------------------------------------------
def test_content_words_drops_stopwords() -> None:
    assert content_words("why is the sky blue") == {"sky", "blue"}
    assert content_words("How do I treat a BLISTER") == {"treat", "blister"}


def test_looks_relevant_rejects_the_real_misfires() -> None:
    """These are the actual top Wikipedia hits for these texts.

    Wikipedia search returns something for any query built from real words, so
    without this gate the tool answered a first-aid question with a summary of a
    travel memoir and a road-closure question with a highway in another province.
    """
    assert looks_relevant("why is the sky blue", "Diffuse sky radiation") is True
    assert looks_relevant("how long to boil water to purify it", "Purified water") is True
    assert looks_relevant("photosynthesis", "Photosynthesis") is True

    assert looks_relevant("how do I treat a blister on a hike", "The Salt Path") is False
    assert looks_relevant("what time is sunset in Tofino", "British Columbia") is False
    assert looks_relevant("best hiking boots", "Vibram S.p.A.") is False


def test_looks_relevant_stem_matching() -> None:
    assert looks_relevant("purify water", "Purified water") is True  # shared 5-char prefix
    assert looks_relevant("open the gate", "Opera house") is False  # 3 chars is not enough


# --- sender allowlist --------------------------------------------------------
def test_allowed_senders_parsing() -> None:
    assert allowed_senders({}) == set()
    parsed = allowed_senders({"ALLOWED_SENDERS": " Me@Example.com , @txt.bell.ca ,, "})
    assert parsed == {"me@example.com", "txt.bell.ca"}


def test_sender_address() -> None:
    msg = {"payload": {"headers": [{"name": "From", "value": "Bob <bob@Example.COM>"}]}}
    assert sender_address(msg) == "bob@example.com"
    assert sender_address({"payload": {"headers": []}}) == ""


def test_sender_permitted() -> None:
    allowed = {"me@example.com", "txt.bell.ca"}
    assert sender_permitted("me@example.com", allowed) is True
    assert sender_permitted("5551234@txt.bell.ca", allowed) is True  # domain match
    assert sender_permitted("stranger@spam.net", allowed) is False
    assert sender_permitted("", allowed) is False
    # An empty allowlist keeps an existing install working; main() warns about it.
    assert sender_permitted("anyone@anywhere.org", set()) is True


# --- gmail payloads ----------------------------------------------------------
def _body(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}


def test_extract_query_prefers_plain_text() -> None:
    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [_body("text/html", "<p>html one</p>"), _body("text/plain", "plain one")],
        }
    }
    assert extract_query(msg) == "plain one"

    html_only = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [_body("text/html", "<p>Rogers MMS  html <b>only</b></p>")],
        }
    }
    assert extract_query(html_only) == "html only"
    assert extract_query({"payload": {}}) is None


def test_extract_query_walks_nested_parts() -> None:
    nested = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "multipart/alternative", "parts": [_body("text/plain", "deep")]}
            ],
        }
    }
    assert extract_query(nested) == "deep"


# --- the poll loop -----------------------------------------------------------
class _Executable:
    """Stands in for the Google client's request objects, which defer until .execute()."""

    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmail:
    """Just enough of the Gmail client to drive process_once. Pages at 100 like the real one.

    Chained the same way as the real client: service.users().messages().list(...).
    """

    PAGE = 100

    def __init__(self, store: dict[str, dict]):
        self.store = store
        self.unread = list(store)
        self.marked_read: list[str] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, labelIds, pageToken=None):  # noqa: N803 - mirrors the Google client
        start = int(pageToken or 0)
        page = self.unread[start : start + self.PAGE]
        resp = {"messages": [{"id": i} for i in page]}
        if start + self.PAGE < len(self.unread):
            resp["nextPageToken"] = str(start + self.PAGE)
        return _Executable(resp)

    def get(self, userId, id):  # noqa: N803 - mirrors the Google client
        return _Executable(self.store[id])

    def modify(self, userId, id, body):  # noqa: N803 - mirrors the Google client
        self.marked_read.append(id)
        return _Executable({})


def _message(sender: str, text: str = "help") -> dict:
    return {
        "payload": {
            "headers": [{"name": "From", "value": sender}],
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        }
    }


def _run_once(messages, **kwargs):
    from RemoteSearch import process_once

    sent: list[str] = []
    service = FakeGmail(messages)
    replied = process_once(service, "LBL", lambda t: (sent.append(t), True)[1], 300, **kwargs)
    return service, sent, replied


def test_unread_ids_follows_pagination() -> None:
    from RemoteSearch import unread_ids

    # 250 unread is three pages. Without following nextPageToken only the first 100
    # were ever seen, so the rest sat unread and arrived together on a later poll.
    service = FakeGmail({f"m{i}": _message("a@b.co") for i in range(250)})
    ids = unread_ids(service, "LBL")
    assert len(ids) == 250, len(ids)
    assert ids[0] == "m249"  # oldest last in Gmail's order, so reversed to oldest first


def test_process_once_caps_replies() -> None:
    # Every reply is a billed SMS, so a flood of mail must not become a flood of texts.
    service, sent, replied = _run_once(
        {f"m{i}": _message("a@b.co") for i in range(50)}, max_replies=3
    )
    assert replied == 3, replied
    assert len(sent) == 3
    assert len(service.marked_read) == 3  # the rest stay unread for the next poll


def test_process_once_enforces_the_allowlist() -> None:
    messages = {
        "ok": _message("5551234@txt.bell.ca"),
        "spam": _message("stranger@spam.net"),
    }
    service, sent, replied = _run_once(messages, allowed={"txt.bell.ca"})
    assert replied == 1, sent
    # The rejected mail is still marked read, or it would be re-checked forever.
    assert sorted(service.marked_read) == ["ok", "spam"]


def test_process_once_survives_a_failed_send() -> None:
    from RemoteSearch import process_once

    service = FakeGmail({"m0": _message("a@b.co"), "m1": _message("a@b.co")})
    replied = process_once(service, "LBL", lambda _t: False, 300)
    assert replied == 0
    assert len(service.marked_read) == 2  # a message that can't be delivered isn't retried forever


# --- formatting --------------------------------------------------------------
def test_place_label() -> None:
    hit = {"name": "Tofino", "admin1": "British Columbia", "country_code": "CA"}
    assert place_label(hit) == "Tofino, British Columbia, CA"
    assert place_label({"name": "Atlantis"}) == "Atlantis"


def test_defaults_are_sane() -> None:
    assert 0 < DEFAULT_MAX_REPLIES <= 100
    assert MAX_QUERY_CHARS > 0


if __name__ == "__main__":
    for _name, _case in sorted(globals().items()):
        if _name.startswith("test_"):
            _case()
            print(f"ok  {_name}")
    print("all passed")
