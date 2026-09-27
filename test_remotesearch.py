"""Offline tests. Run with python test_remotesearch.py, or pytest if it's installed.

Every web API, Gmail and Twilio are faked here, and anything that still reaches for
the network fails the test instead of quietly passing.
"""

import base64
import os
import tempfile
import unicodedata
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from google.auth.exceptions import RefreshError
from twilio.base.exceptions import TwilioRestException

import RemoteSearch
from RemoteSearch import (
    DEFAULT_MAX_REPLIES,
    DEFAULT_SCOPE,
    EMPTY_TEXT,
    HELP_TEXT,
    MAX_QUERY_CHARS,
    ONLINE_TEXT,
    allowed_senders,
    answer,
    authenticate_gmail,
    clean_query,
    content_words,
    extract_query,
    html_to_text,
    load_config,
    looks_relevant,
    main,
    make_sender,
    monitor,
    place_label,
    plain_ascii,
    process_once,
    run_source,
    sender_address,
    sender_permitted,
    setting,
    source_duckduckgo,
    source_stackoverflow,
    source_sun,
    source_weather,
    source_wikipedia,
    strip_refs,
    truncate,
    unread_ids,
)


class NetworkUsed(BaseException):
    """Not an Exception, so the catch-alls in the code under test can't swallow it."""


def _no_network():
    raise NetworkUsed("a test reached for the real network")


real_session = RemoteSearch.session
RemoteSearch.session = _no_network


@contextmanager
def fake_web(responses):
    """Answer get_json from ``responses``, keyed by URL prefix, and record each URL asked for."""
    calls = []

    def get_json(url, **params):
        calls.append((url, params))
        for prefix, value in responses.items():
            if url.startswith(prefix):
                return value
        return None

    with patch("RemoteSearch.get_json", get_json):
        yield calls


WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
DDG = "https://api.duckduckgo.com/"


# --- text -----------------------------------------------------------------------
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


def test_plain_ascii_drops_accents_and_curly_quotes() -> None:
    assert plain_ascii("Montréal, Québec") == "Montreal, Quebec"
    assert plain_ascii("\N{LEFT DOUBLE QUOTATION MARK}hi\N{RIGHT DOUBLE QUOTATION MARK}") == '"hi"'
    assert plain_ascii("it\N{RIGHT SINGLE QUOTATION MARK}s") == "it's"
    for name in ("HYPHEN", "EN DASH", "EM DASH", "HORIZONTAL BAR", "MINUS SIGN"):
        assert plain_ascii(unicodedata.lookup(name)) == "-", name
    assert plain_ascii("wait\N{HORIZONTAL ELLIPSIS}") == "wait..."
    assert plain_ascii("15\N{DEGREE SIGN}C") == "15C"
    assert plain_ascii("plain text") == "plain text"


# --- relevance ------------------------------------------------------------------
def test_content_words_drops_stopwords() -> None:
    assert content_words("why is the sky blue") == {"sky", "blue"}
    assert content_words("How do I treat a BLISTER") == {"treat", "blister"}


def test_looks_relevant_rejects_the_real_misfires() -> None:
    """These are the actual top Wikipedia hits for these texts.

    Wikipedia search returns something for any query built from real words, so without
    this check a first-aid question got a travel memoir and a road question got a
    highway in another province.
    """
    assert looks_relevant("why is the sky blue", "Diffuse sky radiation") is True
    assert looks_relevant("how long to boil water to purify it", "Purified water") is True
    assert looks_relevant("photosynthesis", "Photosynthesis") is True

    assert looks_relevant("how do I treat a blister on a hike", "The Salt Path") is False
    assert looks_relevant("what time is sunset in Tofino", "British Columbia") is False
    assert looks_relevant("best hiking boots", "Vibram S.p.A.") is False


def test_looks_relevant_stem_matching() -> None:
    assert looks_relevant("purify water", "Purified water") is True  # shared 5-letter prefix
    assert looks_relevant("open the gate", "Opera house") is False  # 3 letters isn't enough


def test_looks_relevant_matches_short_word_queries() -> None:
    assert looks_relevant("AC/DC", "AC/DC") is True
    assert looks_relevant("UV", "Ultraviolet") is False


# --- sources --------------------------------------------------------------------
def test_wikipedia_escapes_a_slash_in_the_title() -> None:
    responses = {
        WIKI_SEARCH: {"query": {"search": [{"title": "AC/DC"}]}},
        WIKI_SUMMARY: {"type": "standard", "extract": "AC/DC are an Australian rock band."},
    }
    with fake_web(responses) as calls:
        assert source_wikipedia("AC/DC") == "AC/DC are an Australian rock band."
    assert calls[1][0] == WIKI_SUMMARY + "AC%2FDC"


def test_wikipedia_prefixes_the_article_title() -> None:
    responses = {
        WIKI_SEARCH: {"query": {"search": [{"title": "Purified water"}]}},
        WIKI_SUMMARY: {"type": "standard", "extract": "Water that has been filtered.[1]"},
    }
    with fake_web(responses) as calls:
        result = source_wikipedia("purify water")
    assert result == "Purified water: Water that has been filtered."
    assert calls[1][0] == WIKI_SUMMARY + "Purified_water"


def test_wikipedia_skips_disambiguation_pages() -> None:
    responses = {
        WIKI_SEARCH: {"query": {"search": [{"title": "Mercury"}]}},
        WIKI_SUMMARY: {"type": "disambiguation", "extract": "Mercury may refer to:"},
    }
    with fake_web(responses):
        assert source_wikipedia("mercury") is None


def test_duckduckgo_related_topic_needs_a_matching_title() -> None:
    topics = {
        "AbstractText": "",
        "RelatedTopics": [
            {"FirstURL": "https://duckduckgo.com/Hiking_boot", "Text": "Hiking boot, for blisters"},
            {"FirstURL": "https://duckduckgo.com/Blister", "Text": "Blister A pocket of fluid."},
        ],
    }
    with fake_web({DDG: topics}):
        assert source_duckduckgo("treat a blister") == "Blister A pocket of fluid."
        assert source_duckduckgo("best tent stakes") is None


def test_duckduckgo_prefers_a_direct_answer() -> None:
    with fake_web({DDG: {"Answer": "", "AbstractText": " Albedo is reflectance. "}}):
        assert source_duckduckgo("albedo") == "Albedo is reflectance."


def test_stackoverflow_unescapes_the_title() -> None:
    responses = {
        "https://api.stackexchange.com/2.3/search/advanced": {
            "items": [{"title": "What does &quot;yield&quot; do?", "question_id": 231767}]
        },
        "https://api.stackexchange.com/2.3/questions/231767/answers": {
            "items": [{"body": "<p>It makes a <code>generator</code>.</p>"}]
        },
    }
    with fake_web(responses):
        assert source_stackoverflow("python yield") == 'What does "yield" do? It makes a generator.'


TOFINO = {
    "results": [
        {
            "name": "Tofino",
            "admin1": "British Columbia",
            "country_code": "CA",
            "latitude": 49.15,
            "longitude": -125.9,
        }
    ]
}


def test_weather_reply() -> None:
    current = {
        "temperature_2m": 13.6,
        "apparent_temperature": 11.2,
        "relative_humidity_2m": 88,
        "wind_speed_10m": 14.9,
        "weather_code": 61,
    }
    responses = {
        "https://geocoding-api.open-meteo.com/": TOFINO,
        "https://api.open-meteo.com/v1/forecast": {"current": current},
    }
    with fake_web(responses):
        assert source_weather("Tofino") == (
            "Tofino, British Columbia, CA: light rain, 14C (feels 11C), wind 15km/h, humidity 88%"
        )


def test_sun_reply_and_polar_days() -> None:
    forecast = {
        "timezone_abbreviation": "PDT",
        "daily": {"sunrise": ["2026-09-27T07:14"], "sunset": ["2026-09-27T19:06"]},
    }
    responses = {
        "https://geocoding-api.open-meteo.com/": TOFINO,
        "https://api.open-meteo.com/v1/forecast": forecast,
    }
    with fake_web(responses):
        assert (
            source_sun("Tofino")
            == "Tofino, British Columbia, CA: sunrise 07:14, sunset 19:06 (PDT)"
        )
    forecast["daily"] = {"sunrise": [None], "sunset": [None]}
    with fake_web(responses):
        assert source_sun("Tofino") is None


def test_place_label() -> None:
    hit = {"name": "Tofino", "admin1": "British Columbia", "country_code": "CA"}
    assert place_label(hit) == "Tofino, British Columbia, CA"
    assert place_label({"name": "Atlantis"}) == "Atlantis"


# --- answer ---------------------------------------------------------------------
def test_answer_help() -> None:
    for word in ("help", "HELP", "?"):
        assert answer(word) == HELP_TEXT
    assert answer("") == EMPTY_TEXT
    assert answer("   ") == EMPTY_TEXT


def test_answer_caps_query_length() -> None:
    with fake_web({}) as calls:
        reply = answer("help " + "x" * 5000, limit=300)
    assert len(reply) <= 300
    assert calls, "the long text should still have gone to the web search"
    for _url, params in calls:
        for value in params.values():
            assert len(str(value)) <= MAX_QUERY_CHARS


def test_answer_tags_the_command_that_answered() -> None:
    entries = [
        {
            "meanings": [
                {"partOfSpeech": "noun", "definitions": [{"definition": "Reflectivity."}]},
                {"partOfSpeech": "verb", "definitions": [{"definition": "To reflect."}]},
                {"partOfSpeech": "adjective", "definitions": [{"definition": "Unused."}]},
            ]
        }
    ]
    with fake_web({"https://api.dictionaryapi.dev/": entries}):
        assert answer("define albedo") == "define: (noun) Reflectivity. (verb) To reflect."


def test_answer_does_not_ask_the_same_source_twice() -> None:
    with fake_web({WIKI_SEARCH: {"query": {"search": []}}}) as calls:
        answer("wiki zzqx")
    urls = [url for url, _ in calls]
    assert urls.count(WIKI_SEARCH) == 1
    assert urls.count(DDG) == 1


def test_answer_falls_back_and_folds_accents() -> None:
    with fake_web({DDG: {"AbstractText": "Photosynthèse is photosynthesis in French."}}):
        assert answer("wiki photosynthesis") == "web: Photosynthese is photosynthesis in French."


def test_no_result_reply_keeps_the_advice() -> None:
    question = (
        "how do I treat a blister on a long hike when there is no pharmacy anywhere near "
        "and the only things in the pack are tape, a lighter, a knife, some moleskin and "
        "a very small first aid kit that was mostly used up"
    )
    with fake_web({}):
        reply = answer(question)
    assert reply.startswith("Couldn't find a good answer for 'how do I treat a blister")
    assert reply.endswith("or text help for the commands.")
    assert len(reply) <= 300


def test_run_source_swallows_errors() -> None:
    def boom(_: str) -> str | None:
        raise RuntimeError("network exploded")

    assert run_source(boom, "x") is None
    assert run_source(lambda q: q.upper(), "hi") == "HI"


# --- config ---------------------------------------------------------------------
def test_setting_rejects_bad_numbers() -> None:
    assert setting({}, "POLL_INTERVAL", 5) == 5
    assert setting({"POLL_INTERVAL": ""}, "POLL_INTERVAL", 5) == 5
    assert setting({"POLL_INTERVAL": "12"}, "POLL_INTERVAL", 5) == 12
    for bad in ("0", "-3", "ten", "1.5"):
        try:
            setting({"POLL_INTERVAL": bad}, "POLL_INTERVAL", 5)
        except SystemExit as exc:
            assert repr(bad) in str(exc), exc
        else:
            raise AssertionError(f"{bad!r} was accepted")


def test_load_config_lets_the_environment_win() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.txt"
        # Notepad can save with a byte order mark, which mustn't end up in the first key.
        path.write_text(
            "LABEL_NAME=Texts\n# PHONE_TO=+1\nPOLL_INTERVAL = 9\n", encoding="utf-8-sig"
        )
        with patch.dict(os.environ, {"POLL_INTERVAL": "30"}):
            config = load_config(str(path))
    assert config == {"LABEL_NAME": "Texts", "POLL_INTERVAL": "30"}


def test_allowed_senders_parsing() -> None:
    assert allowed_senders({}) == set()
    parsed = allowed_senders({"ALLOWED_SENDERS": " Me@Example.com , @txt.bell.ca ,, "})
    assert parsed == {"me@example.com", "txt.bell.ca"}


def test_defaults_are_sane() -> None:
    assert 0 < DEFAULT_MAX_REPLIES <= 100
    assert MAX_QUERY_CHARS > 0


def test_session_ignores_retry_after() -> None:
    retry = real_session().get_adapter("https://en.wikipedia.org").max_retries
    assert retry.respect_retry_after_header is False
    assert retry.total == 2


# --- sender allowlist -----------------------------------------------------------
def _from(value: str) -> dict:
    return {"payload": {"headers": [{"name": "From", "value": value}]}}


def test_sender_address() -> None:
    assert sender_address(_from("Bob <bob@Example.COM>")) == "bob@example.com"
    assert sender_address(_from("5551234@txt.bell.ca")) == "5551234@txt.bell.ca"
    assert sender_address({"payload": {"headers": []}}) == ""
    assert sender_address(_from("not an address")) == ""


def test_sender_address_ignores_the_display_name() -> None:
    # A regex would pick the allowed address out of the display name and let these through.
    assert sender_address(_from('"me@example.com" <stranger@spam.net>')) == "stranger@spam.net"
    assert sender_address(_from("me@example.com <stranger@spam.net>")) == ""
    assert sender_address(_from("Me (me@example.com) <stranger@spam.net>")) == "stranger@spam.net"


def test_sender_permitted() -> None:
    allowed = {"me@example.com", "txt.bell.ca"}
    assert sender_permitted("me@example.com", allowed) is True
    assert sender_permitted("5551234@txt.bell.ca", allowed) is True  # domain match
    assert sender_permitted("5551234@evil.txt.bell.ca", allowed) is False
    assert sender_permitted("stranger@spam.net", allowed) is False
    assert sender_permitted("", allowed) is False
    # An empty allowlist lets everyone through, and main() warns about it.
    assert sender_permitted("anyone@anywhere.org", set()) is True


# --- gmail payloads -------------------------------------------------------------
def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _body(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": _encode(text)}}


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


def test_extract_query_accepts_unpadded_base64() -> None:
    data = _encode("sun Tofino!").rstrip("=")
    assert (
        extract_query({"payload": {"mimeType": "text/plain", "body": {"data": data}}})
        == "sun Tofino!"
    )


# --- the poll loop --------------------------------------------------------------
class _Executable:
    """Stands in for the Google client's requests, which wait for .execute()."""

    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmail:
    """Just enough of the Gmail client to drive the poll loop, paging at 100 like the real one.

    It chains the same way the real client does, as in service.users().messages().list(...).
    """

    PAGE = 100

    def __init__(self, store: dict[str, dict]):
        self.store = store
        self.unread = list(store)
        self.marked_read: list[str] = []
        self.events: list[str] = []
        self.list_errors: list[BaseException] = []
        self.modify_error: Exception | None = None

    def users(self):
        return self

    def messages(self):
        return self

    def labels(self):
        labels = [{"id": "LBL", "name": "Remote Server"}]
        return SimpleNamespace(list=lambda userId: _Executable({"labels": labels}))  # noqa: N803

    def list(self, userId, labelIds, maxResults, pageToken=None):  # noqa: N803 - mirrors Google
        if self.list_errors:
            raise self.list_errors.pop(0)
        start = int(pageToken or 0)
        page = self.unread[start : start + self.PAGE]
        resp = {"messages": [{"id": i} for i in page]}
        if start + self.PAGE < len(self.unread):
            resp["nextPageToken"] = str(start + self.PAGE)
        return _Executable(resp)

    def get(self, userId, id):  # noqa: N803 - mirrors Google
        return _Executable(self.store[id])

    def batchModify(self, userId, body):  # noqa: N802, N803 - mirrors Google
        if self.modify_error:
            raise self.modify_error
        self.marked_read.extend(body["ids"])
        self.events.extend(f"read {i}" for i in body["ids"])
        self.unread = [i for i in self.unread if i not in body["ids"]]
        return _Executable({})


def _message(sender: str, text: str = "help") -> dict:
    return {
        "payload": {
            "headers": [{"name": "From", "value": sender}],
            "mimeType": "text/plain",
            "body": {"data": _encode(text)},
        }
    }


def recorder(log: list[str]):
    """A send() that appends each text to ``log`` and reports it delivered."""

    def send(text: str) -> bool:
        log.append(text)
        return True

    return send


def _run_once(messages, **kwargs):
    sent: list[str] = []
    service = FakeGmail(messages)
    replied = process_once(service, "LBL", recorder(sent), 300, **kwargs)
    return service, sent, replied


def test_unread_ids_follows_pagination() -> None:
    # 250 unread is three pages, and without nextPageToken only the first 100 get seen.
    service = FakeGmail({f"m{i}": _message("a@b.co") for i in range(250)})
    ids = unread_ids(service, "LBL")
    assert len(ids) == 250, len(ids)
    assert ids[0] == "m249"  # Gmail lists newest first, so this reverses it to oldest first


def test_process_once_caps_replies() -> None:
    # Every reply is a billed SMS, so a flood of mail can't turn into a flood of texts.
    service, sent, replied = _run_once(
        {f"m{i}": _message("a@b.co") for i in range(50)}, max_replies=3
    )
    assert replied == 3, replied
    assert sent == [HELP_TEXT] * 3
    assert len(service.marked_read) == 3  # the rest stay unread for the next poll


def test_process_once_enforces_the_allowlist() -> None:
    messages = {
        "ok": _message("5551234@txt.bell.ca"),
        "spam": _message("stranger@spam.net"),
        "spoof": _message('"5551234@txt.bell.ca" <stranger@spam.net>'),
    }
    service, sent, replied = _run_once(messages, allowed={"txt.bell.ca"})
    assert replied == 1, sent
    # Rejected mail is still marked read, or it would be checked again on every poll.
    assert sorted(service.marked_read) == ["ok", "spam", "spoof"]


def test_process_once_survives_a_failed_send() -> None:
    service = FakeGmail({"m0": _message("a@b.co"), "m1": _message("a@b.co")})
    replied = process_once(service, "LBL", lambda _t: False, 300)
    assert replied == 0
    assert len(service.marked_read) == 2  # an undeliverable text isn't retried (and billed) forever


def test_process_once_marks_read_before_replying() -> None:
    service = FakeGmail({"m0": _message("a@b.co")})
    process_once(service, "LBL", recorder(service.events), 300)
    assert service.events == ["read m0", HELP_TEXT]


def test_process_once_sends_nothing_when_marking_read_fails() -> None:
    # If this sent first, the message would stay unread and get a new billed reply every poll.
    service = FakeGmail({"m0": _message("a@b.co")})
    service.modify_error = OSError("gmail is down")
    sent: list[str] = []
    assert process_once(service, "LBL", recorder(sent), 300) == 0
    assert sent == []


def test_monitor_backoff_and_revoked_login() -> None:
    service = FakeGmail({})
    service.list_errors = [OSError("gmail is down"), OSError("still down"), RefreshError("revoked")]
    with patch("RemoteSearch.sleep") as sleep:
        try:
            monitor(service, "LBL", lambda _t: True, limit=300, interval=5, catch_up=True)
        except RefreshError:
            pass
        else:
            raise AssertionError("monitor kept going after the login was revoked")
    assert [c.args[0] for c in sleep.call_args_list] == [10, 20]


def test_monitor_clears_the_backlog_without_answering_it() -> None:
    service = FakeGmail({"old1": _message("a@b.co"), "old2": _message("a@b.co")})
    sent: list[str] = []

    def stop_after_first_poll(_seconds):
        service.list_errors.append(RefreshError("stop the loop"))

    with patch("RemoteSearch.sleep", stop_after_first_poll), suppress(RefreshError):
        monitor(service, "LBL", recorder(sent), limit=300, interval=5, catch_up=False)
    assert sorted(service.marked_read) == ["old1", "old2"]
    assert sent == [ONLINE_TEXT]


# --- twilio and gmail setup -----------------------------------------------------
def test_make_sender_sets_a_timeout_and_reports_failures() -> None:
    config = {
        "TWILIO_ACCOUNT_SID": "ACtest",
        "TWILIO_AUTH_TOKEN": "token",
        "TWILIO_PHONE_FROM": "+15550000000",
        "PHONE_TO": "+15551111111",
    }
    with patch("twilio.rest.Client") as client_class:
        send = make_sender(config, dry_run=False)
        client = client_class.return_value
        assert client_class.call_args.kwargs["http_client"].timeout == 10

        client.messages.create.return_value = SimpleNamespace(sid="SM1")
        assert send("hi") is True
        client.messages.create.assert_called_with(
            to="+15551111111", from_="+15550000000", body="hi"
        )

        client.messages.create.side_effect = TwilioRestException(400, "uri", "bad number")
        assert send("hi") is False


def _gmail_config(tmp: str) -> dict[str, str]:
    secrets = Path(tmp) / "credentials.json"
    secrets.write_text("{}", encoding="utf-8")
    token = Path(tmp) / "token.json"
    token.write_text('{"token": "old"}', encoding="utf-8")
    return {"GMAIL_CREDENTIALS_FILE": str(secrets), "GMAIL_TOKEN_FILE": str(token)}


@contextmanager
def fake_google(saved):
    """Patch the Google login pieces, with ``saved`` as the credentials read from the token file."""
    fresh = MagicMock()
    fresh.to_json.return_value = '{"token": "new"}'
    with (
        patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=saved
        ),
        patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file") as flow,
        patch("googleapiclient.discovery.build") as build,
    ):
        flow.return_value.run_local_server.return_value = fresh
        yield flow, build


def test_authenticate_gmail_replaces_a_narrow_token() -> None:
    saved = MagicMock()
    saved.has_scopes.return_value = False
    with tempfile.TemporaryDirectory() as tmp, fake_google(saved) as (flow, build):
        config = _gmail_config(tmp)
        authenticate_gmail(config)
        assert Path(config["GMAIL_TOKEN_FILE"]).read_text(encoding="utf-8") == '{"token": "new"}'
    saved.has_scopes.assert_called_with([DEFAULT_SCOPE])
    assert flow.call_args.args[1] == [DEFAULT_SCOPE]
    assert build.call_args.kwargs["credentials"].to_json() == '{"token": "new"}'


def test_authenticate_gmail_logs_in_again_when_refresh_fails() -> None:
    saved = MagicMock(valid=False)
    saved.has_scopes.return_value = True
    saved.refresh.side_effect = RefreshError("invalid_grant")
    with tempfile.TemporaryDirectory() as tmp, fake_google(saved) as (flow, _build):
        config = _gmail_config(tmp)
        authenticate_gmail(config)
        assert Path(config["GMAIL_TOKEN_FILE"]).read_text(encoding="utf-8") == '{"token": "new"}'
    assert flow.return_value.run_local_server.called


def test_authenticate_gmail_keeps_a_working_token() -> None:
    saved = MagicMock(valid=True)
    saved.has_scopes.return_value = True
    with tempfile.TemporaryDirectory() as tmp, fake_google(saved) as (flow, build):
        config = _gmail_config(tmp)
        authenticate_gmail(config)
        assert Path(config["GMAIL_TOKEN_FILE"]).read_text(encoding="utf-8") == '{"token": "old"}'
    assert not flow.called
    assert build.call_args.kwargs["credentials"] is saved


# --- main -----------------------------------------------------------------------
def test_main_dry_run_needs_no_twilio_settings() -> None:
    service = FakeGmail({"m0": _message("5551234@txt.bell.ca", "define albedo")})
    entries = [
        {"meanings": [{"partOfSpeech": "noun", "definitions": [{"definition": "Reflectivity."}]}]}
    ]
    sent: list[str] = []
    real_make_sender = make_sender

    def spy(config, dry_run):
        assert dry_run is True
        pretend = real_make_sender(config, dry_run)

        def send(text: str) -> bool:
            sent.append(text)
            return pretend(text)

        return send

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.txt"
        path.write_text(
            "GMAIL_CREDENTIALS_FILE=c.json\nGMAIL_TOKEN_FILE=t.json\nALLOWED_SENDERS=@txt.bell.ca\n",
            encoding="utf-8",
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("RemoteSearch.authenticate_gmail", return_value=service),
            patch("RemoteSearch.make_sender", spy),
            fake_web({"https://api.dictionaryapi.dev/": entries}),
        ):
            main(["--config", str(path), "--dry-run", "--once"])
    assert sent == ["define: (noun) Reflectivity."]
    assert service.marked_read == ["m0"]


def test_main_refuses_a_read_only_scope() -> None:
    env = {
        "GMAIL_CREDENTIALS_FILE": "c.json",
        "GMAIL_TOKEN_FILE": "t.json",
        "GMAIL_SCOPE": "https://www.googleapis.com/auth/gmail.readonly",
    }
    with patch.dict(os.environ, env, clear=True), patch("RemoteSearch.authenticate_gmail") as auth:
        try:
            main(["--config", "no-such-file.txt", "--dry-run"])
        except SystemExit as exc:
            assert "gmail.readonly" in str(exc), exc
        else:
            raise AssertionError("a read-only scope was accepted")
    assert not auth.called


if __name__ == "__main__":
    for _name, _case in sorted(globals().items()):
        if _name.startswith("test_"):
            _case()
            print(f"ok  {_name}")
    print("all passed")
