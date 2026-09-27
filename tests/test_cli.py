import json
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from conftest import fake_web
from google.auth.exceptions import RefreshError
from test_gmail import FakeGmail, message
from test_twilio import FakeMessages, text_message

from remotesearch.cli import HourlyCap, main, monitor, process_once
from remotesearch.router import HELP_TEXT, ONLINE_TEXT, Answerer, Incoming, Responder
from remotesearch.state import State

DICTIONARY = "https://api.dictionaryapi.dev/"
ALBEDO = [
    {"meanings": [{"partOfSpeech": "noun", "definitions": [{"definition": "Reflectivity."}]}]}
]
TWILIO = {
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "token",
    "TWILIO_PHONE_FROM": "+17785550000",
}


@contextmanager
def config_file(lines: dict[str, str]) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.txt"
        lines = {"STATE_FILE": str(Path(tmp) / "state.json")} | lines
        path.write_text("".join(f"{k}={v}\n" for k, v in lines.items()), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            yield path


def fake_client(messages: FakeMessages) -> Any:
    return SimpleNamespace(messages=messages)


def test_help_runs() -> None:
    with pytest.raises(SystemExit) as done:
        main(["--help"])
    assert done.value.code == 0


def test_query_prints_the_first_sms_without_any_accounts(capsys: pytest.CaptureFixture) -> None:
    with config_file({}) as path, fake_web({DICTIONARY: ALBEDO}):
        main(["--config", str(path), "--query", "define albedo"])
    assert capsys.readouterr().out == "define: (noun) Reflectivity.\n"
    with config_file({}) as path:
        main(["--config", str(path), "--query", "help"])
    assert capsys.readouterr().out == HELP_TEXT + "\n"


def test_gmail_dry_run_needs_no_twilio_settings() -> None:
    service = FakeGmail({"m0": message("5551234567@txt.bell.ca", "define albedo")})
    settings = {
        "GMAIL_CREDENTIALS_FILE": "c.json",
        "GMAIL_TOKEN_FILE": "t.json",
        "ALLOWED_SENDERS": "@txt.bell.ca",
    }
    with (
        config_file(settings) as path,
        patch("remotesearch.cli.authenticate_gmail", return_value=service),
        patch("remotesearch.cli.twilio_client") as client,
        patch("remotesearch.twilio_sms.logger") as log,
        fake_web({DICTIONARY: ALBEDO}),
    ):
        main(["--config", str(path), "--dry-run", "--once"])
    assert not client.called
    assert service.marked_read == ["m0"]
    log.info.assert_called_with(
        "Dry run, so not texting %s: %s", "PHONE_TO", "define: (noun) Reflectivity."
    )


def test_gmail_startup_clears_the_backlog() -> None:
    service = FakeGmail({"old1": message("a@b.co"), "old2": message("a@b.co")})
    messages = FakeMessages()
    settings = {
        "GMAIL_CREDENTIALS_FILE": "c.json",
        "GMAIL_TOKEN_FILE": "t.json",
        "PHONE_TO": "+16045551234",
        "ALLOWED_SENDERS": "@txt.bell.ca",
        **TWILIO,
    }
    with (
        config_file(settings) as path,
        patch("remotesearch.cli.authenticate_gmail", return_value=service),
        patch("remotesearch.cli.twilio_client", return_value=fake_client(messages)),
        patch("remotesearch.cli.monitor") as loop,
    ):
        main(["--config", str(path)])
    assert sorted(service.marked_read) == ["old1", "old2"]
    assert messages.created == [
        {"to": "+16045551234", "from_": "+17785550000", "body": ONLINE_TEXT}
    ]
    assert loop.call_args.kwargs["interval"] == 5
    assert loop.call_args.kwargs["max_replies"] == 10
    assert loop.call_args.kwargs["hourly"].most == 30


def test_texting_the_twilio_number_directly() -> None:
    messages = FakeMessages()
    messages.stored = [text_message("SM1", "define albedo")]
    settings = {"ALLOWED_SENDERS": "+1 604 555 1234", **TWILIO}
    with (
        config_file(settings) as path,
        patch("remotesearch.cli.twilio_client", return_value=fake_client(messages)),
        fake_web({DICTIONARY: ALBEDO}),
    ):
        main(["--config", str(path), "--once", "--catch-up"])
        state = json.loads(path.with_name("state.json").read_text(encoding="utf-8"))
    assert messages.created == [
        {"to": "+16045551234", "from_": "+17785550000", "body": "define: (noun) Reflectivity."}
    ]
    assert list(state["twilio"]["seen"]) == ["SM1"]


@pytest.mark.parametrize(
    ("settings", "message_text"),
    [
        ({"ALLOWED_SENDERS": "me@example.com"}, "doesn't set up anything to watch"),
        ({"GMAIL_CREDENTIALS_FILE": "c.json", "GMAIL_TOKEN_FILE": "t.json"}, "ALLOWED_SENDERS is"),
        ({**TWILIO, "ALLOWED_SENDERS": ""}, "ALLOWED_SENDERS is empty"),
        ({"ALLOWED_SENDERS": "+16045551234"}, "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN"),
        (
            {"GMAIL_CREDENTIALS_FILE": "c.json", "GMAIL_TOKEN_FILE": "t.json", **TWILIO}
            | {"ALLOWED_SENDERS": "@txt.bell.ca"},
            "sets PHONE_TO",
        ),
        (
            {"GMAIL_CREDENTIALS_FILE": "c.json", "ALLOWED_SENDERS": "@txt.bell.ca"},
            "sets GMAIL_TOKEN_FILE",
        ),
        (
            {"ALLOWED_SENDERS": "+16045551234", "MAX_REPLIES_PER_HOUR": "0", **TWILIO},
            "MAX_REPLIES_PER_HOUR has to be a whole number",
        ),
        (
            {"GMAIL_CREDENTIALS_FILE": "c.json", "MAX_SMS_CHARS": "2000"},
            "won't send more than 1600",
        ),
        ({"ALLOWED_SENDERS": "555-1234"}, "isn't an email address"),
    ],
)
def test_startup_refuses_a_config_it_cant_run(settings: dict[str, str], message_text: str) -> None:
    with config_file(settings) as path, pytest.raises(SystemExit, match=message_text):
        main(["--config", str(path)])


def test_main_refuses_a_read_only_scope() -> None:
    settings = {
        "GMAIL_CREDENTIALS_FILE": "c.json",
        "GMAIL_TOKEN_FILE": "t.json",
        "GMAIL_SCOPE": "https://www.googleapis.com/auth/gmail.readonly",
        "ALLOWED_SENDERS": "@txt.bell.ca",
    }
    with (
        config_file(settings) as path,
        patch("remotesearch.cli.authenticate_gmail") as auth,
        pytest.raises(SystemExit, match=re.escape("gmail.readonly")),
    ):
        main(["--config", str(path), "--dry-run"])
    assert not auth.called


def test_a_revoked_gmail_login_ends_with_advice() -> None:
    service = FakeGmail({})
    service.list_errors = [RefreshError("invalid_grant")]
    settings = {
        "GMAIL_CREDENTIALS_FILE": "c.json",
        "GMAIL_TOKEN_FILE": "t.json",
        "ALLOWED_SENDERS": "@txt.bell.ca",
    }
    with (
        config_file(settings) as path,
        patch("remotesearch.cli.authenticate_gmail", return_value=service),
        pytest.raises(SystemExit, match=re.escape("Delete t.json and run this again")),
    ):
        main(["--config", str(path), "--dry-run", "--once"])


class ListInbox:
    def __init__(self, items: list[Incoming]) -> None:
        self.items = items
        self.handed = 0

    def fetch(self) -> Iterator[Incoming]:
        for item in self.items:
            self.handed += 1
            yield item


def items(sent: list[str], count: int, sender: str = "+16045551234") -> list[Incoming]:
    def send(text: str) -> bool:
        sent.append(text)
        return True

    return [Incoming(sender, "help", "sms", send) for _ in range(count)]


def test_process_once_caps_replies_across_every_inbox() -> None:
    # Every reply is a billed SMS, so a flood of mail can't turn into a flood of texts.
    sent: list[str] = []
    first, second = ListInbox(items(sent, 2)), ListInbox(items(sent, 5))
    responder = Responder(Answerer(), None, 300)
    assert process_once([first, second], responder, max_replies=3) == 3
    assert sent == [HELP_TEXT] * 3
    assert (first.handed, second.handed) == (2, 1)  # the rest stay waiting for the next poll


def test_one_bad_question_doesnt_stop_the_rest() -> None:
    sent: list[str] = []
    responder = Responder(Answerer(), None, 300)
    failing = Incoming("+1", "help", "sms", lambda _t: False)
    with patch.object(Responder, "reply", side_effect=[RuntimeError("bug"), "ok", "ok"]):
        replied = process_once(
            [ListInbox([items(sent, 1)[0], failing, *items(sent, 1)])], responder
        )
    assert replied == 1
    assert sent == ["ok"]


def test_monitor_backs_off_then_stops_on_revoked_login() -> None:
    class Broken:
        def __init__(self) -> None:
            self.errors = [OSError("gmail is down"), OSError("still down"), RefreshError("revoked")]

        def fetch(self) -> Iterator[Incoming]:
            raise self.errors.pop(0)

    with patch("remotesearch.cli.sleep") as sleep, pytest.raises(RefreshError):
        monitor([Broken()], Responder(Answerer(), None, 300), interval=5)
    assert [c.args[0] for c in sleep.call_args_list] == [10, 20]


@pytest.fixture
def state() -> Iterator[State]:
    with tempfile.TemporaryDirectory() as tmp:
        yield State(Path(tmp) / "state.json")


def test_hourly_cap_spans_every_transport(state: State) -> None:
    sent: list[str] = []
    mail, texts = ListInbox(items(sent, 2, "me@example.com")), ListInbox(items(sent, 5))
    hourly = HourlyCap(state, 3)
    assert process_once([mail, texts], Responder(Answerer(), None, 300), 10, hourly) == 3
    assert (mail.handed, texts.handed) == (2, 1)
    assert len(json.loads(state.path.read_text(encoding="utf-8"))["sent"]) == 3


def test_the_hourly_cap_survives_a_restart(state: State) -> None:
    sent: list[str] = []
    process_once(
        [ListInbox(items(sent, 3))], Responder(Answerer(), None, 300), 10, HourlyCap(state, 3)
    )
    later = ListInbox(items(sent, 1))
    restarted = HourlyCap(State(state.path), 3)
    assert process_once([later], Responder(Answerer(), None, 300), 10, restarted) == 0
    assert later.handed == 0  # still unread or unseen, so it's answered once there's room


def test_replies_older_than_an_hour_dont_count(state: State) -> None:
    old = (datetime.now(UTC) - timedelta(minutes=61)).isoformat()
    state.data["sent"] = [old] * 3
    sent: list[str] = []
    assert (
        process_once(
            [ListInbox(items(sent, 1))], Responder(Answerer(), None, 300), 10, HourlyCap(state, 3)
        )
        == 1
    )
    assert len(state.data["sent"]) == 1
    assert state.data["sent"][0] != old


def test_failed_sends_dont_count_toward_the_hour(state: State) -> None:
    failing = [Incoming("+1", "help", "sms", lambda _t: False)] * 4
    hourly = HourlyCap(state, 3)
    assert process_once([ListInbox(failing)], Responder(Answerer(), None, 300), 10, hourly) == 0
    assert state.data.get("sent", []) == []


def test_the_hourly_cap_is_logged_once_per_stretch(
    state: State, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, "remotesearch")
    state.data["sent"] = [datetime.now(UTC).isoformat()] * 3
    hourly = HourlyCap(state, 3)
    responder = Responder(Answerer(), None, 300)
    for _ in range(3):
        process_once([ListInbox(items([], 1))], responder, 10, hourly)
    assert caplog.text.count("MAX_REPLIES_PER_HOUR") == 1
    state.data["sent"] = []
    process_once([ListInbox(items([], 1))], responder, 10, hourly)
    state.data["sent"] = [datetime.now(UTC).isoformat()] * 3
    process_once([ListInbox(items([], 1))], responder, 10, hourly)
    assert caplog.text.count("MAX_REPLIES_PER_HOUR") == 2
