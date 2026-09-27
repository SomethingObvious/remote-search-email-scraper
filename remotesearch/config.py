"""Settings from config.txt and the environment, and the sender allowlist."""

import os
import re
from pathlib import Path

GMAIL_KEYS = ("GMAIL_CREDENTIALS_FILE", "GMAIL_TOKEN_FILE")
TWILIO_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_FROM")
OPTIONAL_KEYS = (
    "PHONE_TO",
    "GMAIL_SCOPE",
    "LABEL_NAME",
    "POLL_INTERVAL",
    "MAX_SMS_CHARS",
    "ALLOWED_SENDERS",
    "MAX_REPLIES_PER_POLL",
    "MAX_REPLIES_PER_HOUR",
    "REPLY_BY_SMS",
    "STATE_FILE",
    "BRAVE_API_KEY",
    "NEWS_REGION",
    "AI_API_KEY",
    "AI_PROVIDER",
    "AI_MODEL",
    "AI_BASE_URL",
)

# E.164, which is how Twilio reports every number, as in +15551234567.
PHONE = re.compile(r"\+[1-9]\d{6,14}")


def load_config(path: str) -> dict[str, str]:
    """Read ``KEY=value`` lines from ``path`` if it exists, with environment variables winning."""
    config: dict[str, str] = {}
    file = Path(path)
    if file.exists():
        for raw in file.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    for key in (*GMAIL_KEYS, *TWILIO_KEYS, *OPTIONAL_KEYS):
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def require(config: dict[str, str], keys: tuple[str, ...], path: str) -> None:
    missing = [k for k in keys if not config.get(k)]
    if missing:
        raise SystemExit(
            f"Neither {path} nor the environment sets {', '.join(missing)}. "
            "config.example.txt shows what each one is."
        )


def setting(config: dict[str, str], key: str, default: int) -> int:
    """Read a config value that has to be a whole number of 1 or more."""
    raw = config.get(key) or str(default)
    if not raw.isdecimal() or int(raw) < 1:
        raise SystemExit(f"{key} has to be a whole number of 1 or more, not {raw!r}.")
    return int(raw)


def phone_number(text: str) -> str | None:
    """Return ``text`` as a bare E.164 number, or None when it isn't one."""
    number = re.sub(r"[\s().-]", "", text)
    return number if PHONE.fullmatch(number) else None


def sender_list(config: dict[str, str], key: str = "ALLOWED_SENDERS") -> set[str]:
    """Parse a comma-separated list of addresses, bare domains and +phone numbers."""
    entries = set()
    for part in config.get(key, "").split(","):
        entry = part.strip().lower().lstrip("@")
        if not entry:
            continue
        if entry.startswith("+"):
            number = phone_number(entry)
            if not number:
                raise SystemExit(
                    f"{key} has {part.strip()!r}, which isn't a phone number in the "
                    "+15551234567 form."
                )
            entry = number
        elif "." not in entry.rpartition("@")[2]:
            raise SystemExit(
                f"{key} has {part.strip()!r}, which isn't an email address, a domain "
                "or a +15551234567 phone number."
            )
        entries.add(entry)
    return entries


def allowed_senders(config: dict[str, str]) -> set[str]:
    return sender_list(config, "ALLOWED_SENDERS")


def sender_permitted(sender: str, allowed: set[str]) -> bool:
    """Match an email address by itself or its domain, or an E.164 number, against the list."""
    if sender.startswith("+"):
        return sender in allowed
    domain = sender.rpartition("@")[2]
    return sender in allowed or (bool(domain) and domain in allowed)
