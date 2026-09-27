"""The Gmail transport: mail under a label in, an SMS or an email reply out."""

import base64
import logging
import re
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError

from .config import sender_permitted
from .router import Incoming
from .text import clean_query, html_to_text

logger = logging.getLogger("remotesearch")

DEFAULT_LABEL = "Remote Server"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# Marking mail read and sending replies need one of these, and both include sending.
MODIFY_SCOPES = (DEFAULT_SCOPE, "https://mail.google.com/")
# A carrier's SMS-to-email gateway sends from the phone's own number, like
# 6045551234@txt.bell.ca, so mail from an address like that gets its answer by SMS.
PHONE_GATEWAY = re.compile(r"\+?1?\d{10}")
NO_REPLY = re.compile(r"no[._-]?reply|do[._-]?not[._-]?reply|mailer-daemon|postmaster")


def authenticate_gmail(config: dict[str, str]) -> Any:
    """Build a Gmail client, asking for a browser login only when the saved one can't be used."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = [config.get("GMAIL_SCOPE") or DEFAULT_SCOPE]
    token = Path(config["GMAIL_TOKEN_FILE"])
    # Loaded with the scopes it was granted, so a token saved under a narrower scope
    # is replaced here instead of failing to mark each message read.
    creds = Credentials.from_authorized_user_file(str(token)) if token.exists() else None
    if creds and not creds.has_scopes(scopes):
        creds = None
    if creds and creds.valid:
        return build("gmail", "v1", credentials=creds)
    if creds:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            logger.warning("The saved Gmail login stopped working (%s), so it needs a new one", exc)
            creds = None
    if not creds:
        secrets = config["GMAIL_CREDENTIALS_FILE"]
        if not Path(secrets).exists():
            raise SystemExit(
                f"Couldn't find {secrets}. Download the Desktop app OAuth client JSON from the "
                "Google Cloud console and point GMAIL_CREDENTIALS_FILE at it."
            )
        creds = InstalledAppFlow.from_client_secrets_file(secrets, scopes).run_local_server(port=0)
    # It holds a refresh token, so nobody else on the machine should be able to read it.
    token.touch(mode=0o600)
    token.chmod(0o600)
    token.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def get_label_id(service: Any, label_name: str) -> str | None:
    """Resolve a Gmail label's display name to its ID, ignoring case."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == label_name.lower():
            return str(label["id"])
    return None


def _find_body(part: dict[str, Any], mime: str) -> str | None:
    """Walk a possibly nested multipart payload for the first body of type ``mime``."""
    data = part.get("body", {}).get("data")
    if part.get("mimeType") == mime and data:
        # urlsafe_b64decode refuses data without its = padding, so it's padded here in
        # case Gmail leaves it off.
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    for sub in part.get("parts") or []:
        found = _find_body(sub, mime)
        if found:
            return found
    return None


def extract_query(message: dict[str, Any]) -> str | None:
    """Pull the text out of a message, preferring plain text over HTML."""
    payload = message.get("payload", {})
    plain = _find_body(payload, "text/plain")
    if plain:
        return clean_query(plain)
    markup = _find_body(payload, "text/html")
    return html_to_text(markup) if markup else None


def header(message: dict[str, Any], name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    return next((h.get("value", "") for h in headers if h.get("name", "").lower() == name), "")


def sender_address(message: dict[str, Any]) -> str:
    """The From address, lowercased, or an empty string when there isn't one clear address."""
    # parseaddr takes the address in the angle brackets. A plain regex takes the first
    # thing shaped like an address, which can be a fake one in the display name.
    address = parseaddr(header(message, "from"))[1].lower()
    return address if "@" in address else ""


def unread_ids(service: Any, label_id: str) -> list[str]:
    """IDs of unread messages under the label, oldest first, across every page."""
    ids: list[str] = []
    token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", labelIds=[label_id, "UNREAD"], maxResults=500, pageToken=token)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            return list(reversed(ids))


def mark_read(service: Any, ids: list[str]) -> None:
    for start in range(0, len(ids), 1000):  # batchModify takes up to 1000 IDs a call
        body = {"ids": ids[start : start + 1000], "removeLabelIds": ["UNREAD"]}
        service.users().messages().batchModify(userId="me", body=body).execute()


def reply_email(message: dict[str, Any], text: str) -> dict[str, str]:
    """The Gmail API body for an answer in the same thread as ``message``."""
    reply = EmailMessage()
    reply["To"] = sender_address(message)
    subject = re.sub(r"\s+", " ", header(message, "subject")).strip() or "your question"
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    message_id = header(message, "message-id").strip()
    if message_id:
        reply["In-Reply-To"] = message_id
        reply["References"] = message_id
    reply.set_content(text)
    body = {"raw": base64.urlsafe_b64encode(reply.as_bytes()).decode("ascii")}
    if message.get("threadId"):
        body["threadId"] = message["threadId"]
    return body


class GmailInbox:
    """Unread mail under one label, each message marked read before it's handed on."""

    def __init__(
        self,
        service: Any,
        label_id: str,
        text_phone: Callable[[str], bool],
        *,
        allowed: set[str],
        sms_senders: set[str],
        token_file: str,
        dry_run: bool = False,
    ) -> None:
        self.service = service
        self.label_id = label_id
        self.text_phone = text_phone
        self.allowed = allowed
        self.sms_senders = sms_senders
        self.token_file = token_file
        self.dry_run = dry_run

    def answers_by_sms(self, sender: str) -> bool:
        """Carrier gateways and REPLY_BY_SMS get a text to PHONE_TO, and other mail gets email."""
        if sender_permitted(sender, self.sms_senders):
            return True
        return bool(PHONE_GATEWAY.fullmatch(sender.partition("@")[0]))

    def email(self, message: dict[str, Any], text: str) -> bool:
        body = reply_email(message, text)
        to = sender_address(message)
        if self.dry_run:
            logger.info("Dry run, so not emailing %s: %s", to, text)
            return True
        from googleapiclient.errors import HttpError

        try:
            self.service.users().messages().send(userId="me", body=body).execute()
        except HttpError as exc:
            if exc.resp.status == 403:
                logger.error(
                    "Gmail won't send mail with the saved login, as it was granted without "
                    "permission to send. Delete %s and run this again to log back in with %s.",
                    self.token_file,
                    DEFAULT_SCOPE,
                )
            else:
                logger.error("Couldn't email %s: %s", to, exc)
            return False
        return True

    def fetch(self) -> Iterator[Incoming]:
        for msg_id in unread_ids(self.service, self.label_id):
            try:
                message = self.service.users().messages().get(userId="me", id=msg_id).execute()
                # Marked read before the reply goes out. The other way round, a message
                # that can't be marked gets answered (and billed) again on every poll.
                mark_read(self.service, [msg_id])
                incoming = self._incoming(msg_id, message)
            except RefreshError:
                raise
            except Exception as exc:  # one bad message shouldn't hold up the rest of the batch
                logger.error("Couldn't handle message %s: %s", msg_id, exc)
                continue
            if incoming:
                yield incoming

    def _incoming(self, msg_id: str, message: dict[str, Any]) -> Incoming | None:
        sender = sender_address(message)
        query = extract_query(message)
        if not sender_permitted(sender, self.allowed):
            logger.warning("Ignoring mail from %r as it isn't in ALLOWED_SENDERS", sender)
            return None
        if not query:
            logger.warning("Message %s had no text to answer", msg_id)
            return None
        if self.answers_by_sms(sender):
            return Incoming(sender, query, "sms", self.text_phone)
        if NO_REPLY.search(sender.partition("@")[0]):
            # Garmin inReach mail comes from no.reply.inreach@garmin.com, and only the
            # link inside it reaches the device.
            logger.warning(
                "Not answering %s, as it's a no-reply address and an email back would go "
                "nowhere. Text the Twilio number from that device instead.",
                sender,
            )
            return None
        return Incoming(sender, query, "email", lambda text: self.email(message, text))
