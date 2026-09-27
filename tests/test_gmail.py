import base64
import email
import logging
import tempfile
from contextlib import contextmanager
from email import policy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from remotesearch.gmail import (
    DEFAULT_SCOPE,
    GmailInbox,
    authenticate_gmail,
    extract_query,
    reply_email,
    sender_address,
    unread_ids,
)


def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _body(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": _encode(text)}}


def _from(value: str) -> dict:
    return {"payload": {"headers": [{"name": "From", "value": value}]}}


def message(sender: str, text: str = "help", subject: str = "", thread: str = "T1") -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "Message-ID", "value": "<q1@mail>"}]
    if subject:
        headers.append({"name": "Subject", "value": subject})
    return {
        "threadId": thread,
        "payload": {"headers": headers, "mimeType": "text/plain", "body": {"data": _encode(text)}},
    }


class _Executable:
    """Stands in for the Google client's requests, which wait for .execute()."""

    def __init__(self, value: Any, error: Exception | None = None) -> None:
        self.value = value
        self.error = error

    def execute(self) -> Any:
        if self.error:
            raise self.error
        return self.value


class FakeGmail:
    """Just enough of the Gmail client to drive the inbox, paging at 100 like the real one.

    It chains the same way the real client does, as in service.users().messages().list(...).
    """

    PAGE = 100

    def __init__(self, store: dict[str, dict]) -> None:
        self.store = store
        self.unread = list(store)
        self.marked_read: list[str] = []
        self.sent: list[dict] = []
        self.events: list[str] = []
        self.list_errors: list[BaseException] = []
        self.modify_error: Exception | None = None
        self.send_error: Exception | None = None

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
        resp: dict[str, Any] = {"messages": [{"id": i} for i in page]}
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

    def send(self, userId, body):  # noqa: N803 - mirrors Google
        self.sent.append(body)
        return _Executable({"id": "sent1"}, self.send_error)


def inbox(service: FakeGmail, texts: list[str], **kwargs: Any) -> GmailInbox:
    def text_phone(body: str) -> bool:
        texts.append(body)
        service.events.append(f"sms {body}")
        return True

    options: dict[str, Any] = {"allowed": {"txt.bell.ca"}, "sms_senders": set()}
    options |= {"token_file": "token.json"} | kwargs
    return GmailInbox(service, "LBL", text_phone, **options)


# --- reading messages ---------------------------------------------------------
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


def test_extract_query_walks_nested_parts_and_unpadded_base64() -> None:
    nested = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "multipart/alternative", "parts": [_body("text/plain", "deep")]}
            ],
        }
    }
    assert extract_query(nested) == "deep"
    data = _encode("sun Tofino!").rstrip("=")
    unpadded = {"payload": {"mimeType": "text/plain", "body": {"data": data}}}
    assert extract_query(unpadded) == "sun Tofino!"


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


def test_unread_ids_follows_pagination() -> None:
    # 250 unread is three pages, and without nextPageToken only the first 100 get seen.
    service = FakeGmail({f"m{i}": message("a@b.co") for i in range(250)})
    ids = unread_ids(service, "LBL")
    assert len(ids) == 250, len(ids)
    assert ids[0] == "m249"  # Gmail lists newest first, so this reverses it to oldest first


# --- the inbox ----------------------------------------------------------------
def test_gateway_mail_gets_an_sms_reply() -> None:
    texts: list[str] = []
    service = FakeGmail({"m0": message("6045551234@txt.bell.ca", "define albedo")})
    [incoming] = list(inbox(service, texts, allowed={"txt.bell.ca"}).fetch())
    assert (incoming.sender, incoming.text, incoming.channel) == (
        "6045551234@txt.bell.ca",
        "define albedo",
        "sms",
    )
    assert incoming.send("an answer") is True
    assert texts == ["an answer"]
    assert service.sent == []


def test_plain_email_gets_an_email_reply() -> None:
    service = FakeGmail({"m0": message("Me <me@example.com>", "sun Tofino", "trail question")})
    [incoming] = list(inbox(service, [], allowed={"me@example.com"}).fetch())
    assert incoming.channel == "email"
    assert incoming.send("Tofino: sunrise 07:17") is True
    [sent] = service.sent
    assert sent["threadId"] == "T1"
    mail = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"]), policy=policy.default)
    assert mail["To"] == "me@example.com"
    assert mail["Subject"] == "Re: trail question"
    assert mail["In-Reply-To"] == "<q1@mail>"
    assert mail["References"] == "<q1@mail>"
    assert mail.get_content().strip() == "Tofino: sunrise 07:17"


def test_reply_email_subject() -> None:
    body = reply_email(message("me@example.com", subject="RE:  a\r\n  question"), "x")
    mail = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)
    assert mail["Subject"] == "RE: a question"
    bare = message("me@example.com")
    bare.pop("threadId")
    body = reply_email(bare, "x")
    assert "threadId" not in body
    mail = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)
    assert mail["Subject"] == "Re: your question"


def test_reply_by_sms_overrides_email_for_listed_senders() -> None:
    texts: list[str] = []
    service = FakeGmail({"m0": message("me@example.com"), "m1": message("you@example.com")})
    options = {"allowed": {"example.com"}, "sms_senders": {"me@example.com"}}
    channels = [(i.sender, i.channel) for i in inbox(service, texts, **options).fetch()]
    assert channels == [("you@example.com", "email"), ("me@example.com", "sms")]


def test_no_reply_senders_get_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, "remotesearch")
    service = FakeGmail({"m0": message("no.reply.inreach@garmin.com", "weather Squamish")})
    assert list(inbox(service, [], allowed={"garmin.com"}).fetch()) == []
    assert service.marked_read == ["m0"]
    assert "Not answering no.reply.inreach@garmin.com" in caplog.text


def test_an_empty_allowlist_answers_nobody() -> None:
    service = FakeGmail({"m0": message("anyone@example.com"), "m1": message("Bob <>")})
    assert list(inbox(service, [], allowed=set()).fetch()) == []
    assert sorted(service.marked_read) == ["m0", "m1"]


def test_the_allowlist_is_checked_on_the_real_address() -> None:
    messages = {
        "ok": message("5551234567@txt.bell.ca"),
        "spam": message("stranger@spam.net"),
        "spoof": message('"5551234567@txt.bell.ca" <stranger@spam.net>'),
    }
    service = FakeGmail(messages)
    incoming = list(inbox(service, [], allowed={"txt.bell.ca"}).fetch())
    assert [i.sender for i in incoming] == ["5551234567@txt.bell.ca"]
    # Rejected mail is still marked read, or it would be checked again on every poll.
    assert sorted(service.marked_read) == ["ok", "spam", "spoof"]


def test_mail_with_no_text_is_skipped() -> None:
    blank = {
        "threadId": "T",
        "payload": {"headers": [{"name": "From", "value": "6045551234@txt.bell.ca"}]},
    }
    service = FakeGmail({"m0": blank})
    assert list(inbox(service, []).fetch()) == []
    assert service.marked_read == ["m0"]


def test_messages_are_marked_read_first() -> None:
    service = FakeGmail(
        {"m0": message("6045551234@txt.bell.ca"), "m1": message("6045551234@txt.bell.ca")}
    )
    for incoming in inbox(service, []).fetch():
        incoming.send("reply")
    # Gmail lists newest first, and the inbox hands them on oldest first.
    assert service.events == ["read m1", "sms reply", "read m0", "sms reply"]


def test_nothing_is_handed_on_when_marking_read_fails() -> None:
    # If this answered first, the message would stay unread and get a new billed reply every poll.
    service = FakeGmail({"m0": message("6045551234@txt.bell.ca")})
    service.modify_error = OSError("gmail is down")
    assert list(inbox(service, []).fetch()) == []


def test_a_revoked_login_stops_the_inbox() -> None:
    service = FakeGmail({"m0": message("6045551234@txt.bell.ca")})
    service.modify_error = RefreshError("revoked")
    with pytest.raises(RefreshError):
        list(inbox(service, []).fetch())


def test_an_email_reply_without_send_permission_explains_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, "remotesearch")
    service = FakeGmail({"m0": message("me@example.com")})
    service.send_error = HttpError(
        SimpleNamespace(status=403, reason="Forbidden"),
        b'{"error": {"message": "Request had insufficient authentication scopes."}}',
    )
    [incoming] = list(inbox(service, [], allowed={"me@example.com"}).fetch())
    assert incoming.send("answer") is False
    assert "Delete token.json and run this again to log back in with" in caplog.text
    service.send_error = HttpError(SimpleNamespace(status=500, reason="Oops"), b"{}")
    assert incoming.send("answer") is False
    assert "Couldn't email me@example.com" in caplog.text


def test_a_dry_run_logs_the_email_instead() -> None:
    service = FakeGmail({"m0": message("me@example.com")})
    [incoming] = list(inbox(service, [], allowed={"me@example.com"}, dry_run=True).fetch())
    assert incoming.send("answer") is True
    assert service.sent == []


# --- logging in ---------------------------------------------------------------
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


def test_authenticate_gmail_needs_the_client_file() -> None:
    with tempfile.TemporaryDirectory() as tmp, fake_google(None):
        config = _gmail_config(tmp)
        Path(config["GMAIL_CREDENTIALS_FILE"]).unlink()
        with pytest.raises(SystemExit, match="Couldn't find"):
            authenticate_gmail(config)
