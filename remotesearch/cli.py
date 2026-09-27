"""Command line, startup checks and the poll loop over every transport."""

import argparse
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from time import sleep
from typing import Any, Protocol

from google.auth.exceptions import RefreshError

from .config import (
    GMAIL_KEYS,
    TWILIO_KEYS,
    allowed_senders,
    load_config,
    require,
    sender_list,
    setting,
)
from .gmail import (
    DEFAULT_LABEL,
    DEFAULT_SCOPE,
    MODIFY_SCOPES,
    GmailInbox,
    authenticate_gmail,
    get_label_id,
    unread_ids,
)
from .router import DEFAULT_SMS_CHARS, ONLINE_TEXT, Answerer, Incoming, Responder, answer
from .state import DEFAULT_STATE_FILE, State
from .twilio_sms import TwilioInbox, make_texter, twilio_client

logger = logging.getLogger("remotesearch")

TWILIO_MAX_CHARS = 1600  # Twilio rejects a longer body outright
DEFAULT_MAX_REPLIES = 10  # per poll, and every reply is a billed SMS
DEFAULT_MAX_PER_HOUR = 30
DEFAULT_INTERVAL = 5


class Inbox(Protocol):
    def fetch(self) -> Iterable[Incoming]: ...


class HourlyCap:
    """Replies sent in the last hour across every transport, kept in the state file.

    The per-poll cap alone lets a steady trickle of texts run up the bill, and a
    restart would reset a count kept in memory.
    """

    def __init__(self, state: State, most: int) -> None:
        self.state = state
        self.most = most
        self.warned = False

    def _recent(self) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        sent = [t for t in self.state.data.get("sent", []) if datetime.fromisoformat(t) > cutoff]
        self.state.data["sent"] = sent
        return sent

    def reached(self) -> bool:
        if len(self._recent()) < self.most:
            self.warned = False
            return False
        if not self.warned:  # once per stretch at the cap, not once per poll
            logger.warning(
                "Sent %d replies in the last hour, which is MAX_REPLIES_PER_HOUR, so new "
                "questions wait until the hour has room again",
                self.most,
            )
            self.warned = True
        return True

    def count(self) -> None:
        self._recent().append(datetime.now(UTC).isoformat())
        self.state.save()


def process_once(
    inboxes: list[Inbox],
    responder: Responder,
    max_replies: int = DEFAULT_MAX_REPLIES,
    hourly: HourlyCap | None = None,
) -> int:
    """Answer what's waiting in every inbox, up to the caps, and return how many went out.

    Both caps are checked before the next message is taken, so whatever's left stays
    unread in Gmail or unseen on Twilio, and gets its answer on a later poll.
    """
    replied = 0
    for inbox in inboxes:
        if hourly and hourly.reached():
            return replied
        for incoming in inbox.fetch():
            try:
                logger.info(
                    "Question from %s: %s", incoming.sender or "an unknown sender", incoming.text
                )
                reply = responder.reply(incoming.sender, incoming.text, incoming.channel)
                if incoming.send(reply):
                    replied += 1
                    if hourly:
                        hourly.count()
            except Exception as exc:  # one bad question shouldn't hold up the rest
                logger.error("Couldn't answer %s: %s", incoming.sender, exc)
            if replied >= max_replies:
                logger.warning(
                    "Sent %d replies this poll, which is the cap, so the rest wait for the next",
                    max_replies,
                )
                return replied
            if hourly and hourly.reached():
                return replied
    return replied


def monitor(
    inboxes: list[Inbox],
    responder: Responder,
    *,
    interval: int,
    max_replies: int = DEFAULT_MAX_REPLIES,
    hourly: HourlyCap | None = None,
) -> None:
    """Poll every inbox forever, backing off while they keep failing."""
    failures = 0
    while True:
        try:
            process_once(inboxes, responder, max_replies, hourly)
            failures = 0
        except RefreshError:
            raise  # retrying can't fix a revoked login, so main() explains it and exits
        except Exception as exc:  # Gmail and Twilio have short outages, and one shouldn't stop this
            failures += 1
            logger.error("Poll failed (%d in a row): %s", failures, exc)
        # It backs off to 5 minutes while polls keep failing instead of hammering them.
        sleep(max(interval, min(interval * 2**failures, 300)))


def positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"has to be 1 or more, not {value}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Answer questions texted from a phone with no data, by SMS or email."
    )
    parser.add_argument("--config", default="config.txt", help="path to the config file")
    parser.add_argument("--query", help="answer one question and exit, with no accounts needed")
    parser.add_argument("--once", action="store_true", help="answer what's waiting and exit")
    parser.add_argument("--catch-up", action="store_true", help="answer what's waiting at startup")
    parser.add_argument("--interval", type=positive, help="seconds between polls")
    parser.add_argument("--max-chars", type=positive, help="longest SMS reply in characters")
    parser.add_argument("--max-replies", type=positive, help="most replies sent per poll")
    parser.add_argument("--dry-run", action="store_true", help="log replies instead of sending")
    parser.add_argument("--verbose", action="store_true", help="log debug detail")
    return parser.parse_args(argv)


def gmail_inbox(
    config: dict[str, str],
    args: argparse.Namespace,
    text: Callable[[str, str], bool],
    allowed: set[str],
) -> GmailInbox:
    scope = config.get("GMAIL_SCOPE") or DEFAULT_SCOPE
    if scope not in MODIFY_SCOPES:
        raise SystemExit(
            f"GMAIL_SCOPE is {scope}, which can't mark mail read or send replies. "
            f"Use {DEFAULT_SCOPE}."
        )
    service = authenticate_gmail(config)
    label_name = config.get("LABEL_NAME") or DEFAULT_LABEL
    label_id = get_label_id(service, label_name)
    if not label_id:
        raise SystemExit(
            f"There's no Gmail label called {label_name!r}. Create it, or set LABEL_NAME "
            "to the label your gateway filter applies."
        )
    phone = config.get("PHONE_TO", "")
    text_phone = partial(text, phone)
    inbox = GmailInbox(
        service,
        label_id,
        text_phone,
        allowed=allowed,
        sms_senders=sender_list(config, "REPLY_BY_SMS"),
        token_file=config["GMAIL_TOKEN_FILE"],
        dry_run=args.dry_run,
    )
    if not args.catch_up and not args.once:
        inbox.mark_handled(unread_ids(service, label_id))
        if phone:
            text_phone(ONLINE_TEXT)
    logger.info("Checking the %r Gmail label", label_name)
    return inbox


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # Only this script logs at INFO. Twilio's client logs every request at INFO,
    # account SID included.
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    config = load_config(args.config)
    limit = args.max_chars or setting(config, "MAX_SMS_CHARS", DEFAULT_SMS_CHARS)
    if args.query is not None:
        print(answer(args.query, limit, config))
        return
    if limit > TWILIO_MAX_CHARS:
        raise SystemExit(
            f"The reply limit is {limit} characters, but Twilio won't send more than "
            f"{TWILIO_MAX_CHARS}. Lower MAX_SMS_CHARS or --max-chars."
        )

    allowed = allowed_senders(config)
    if not allowed:
        raise SystemExit(
            "ALLOWED_SENDERS is empty, so anyone who found the address or number would get "
            "billed replies. Set it to your +phone number, your carrier's gateway domain or "
            "the email addresses that can ask."
        )
    use_gmail = any(config.get(k) for k in GMAIL_KEYS)
    use_twilio = any(s.startswith("+") for s in allowed)
    if not use_gmail and not use_twilio:
        raise SystemExit(
            f"{args.config} doesn't set up anything to watch. Set GMAIL_CREDENTIALS_FILE and "
            "GMAIL_TOKEN_FILE for mail, or put your +phone number in ALLOWED_SENDERS to text "
            "the Twilio number directly."
        )
    needed: tuple[str, ...] = GMAIL_KEYS if use_gmail else ()
    if use_twilio or not args.dry_run:
        needed += TWILIO_KEYS
    if use_gmail and not args.dry_run:
        needed += ("PHONE_TO",)
    require(config, needed, args.config)
    interval = args.interval or setting(config, "POLL_INTERVAL", DEFAULT_INTERVAL)
    max_replies = args.max_replies or setting(config, "MAX_REPLIES_PER_POLL", DEFAULT_MAX_REPLIES)
    per_hour = setting(config, "MAX_REPLIES_PER_HOUR", DEFAULT_MAX_PER_HOUR)
    logger.info("Answering %s only", ", ".join(sorted(allowed)))

    client: Any = twilio_client(config) if use_twilio or not args.dry_run else None
    text = make_texter(None if args.dry_run else client, config.get("TWILIO_PHONE_FROM", ""))
    state = State(Path(config.get("STATE_FILE") or DEFAULT_STATE_FILE), dry_run=args.dry_run)
    responder = Responder(Answerer(config, limit), state, limit)
    # Dry runs send nothing, so they don't use up the hour's replies.
    hourly = None if args.dry_run else HourlyCap(state, per_hour)
    try:
        inboxes: list[Inbox] = []
        if use_gmail:
            inboxes.append(gmail_inbox(config, args, text, allowed))
        if use_twilio:
            logger.info("Checking texts to %s", config["TWILIO_PHONE_FROM"])
            inboxes.append(
                TwilioInbox(
                    client,
                    config["TWILIO_PHONE_FROM"],
                    state,
                    text,
                    allowed=allowed,
                    catch_up=args.catch_up,
                )
            )
        if args.once:
            count = process_once(inboxes, responder, max_replies, hourly)
            logger.info("Sent %d %s", count, "reply" if count == 1 else "replies")
        else:
            monitor(inboxes, responder, interval=interval, max_replies=max_replies, hourly=hourly)
    except RefreshError as exc:
        raise SystemExit(
            f"Gmail turned down the saved login ({exc}). Delete {config['GMAIL_TOKEN_FILE']} "
            "and run this again to log back in."
        ) from None
    except KeyboardInterrupt:
        pass  # Ctrl-C is how it's meant to be stopped
