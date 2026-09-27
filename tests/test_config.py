import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from remotesearch.config import (
    allowed_senders,
    load_config,
    phone_number,
    sender_list,
    sender_permitted,
    setting,
)


def test_setting_rejects_bad_numbers() -> None:
    assert setting({}, "POLL_INTERVAL", 5) == 5
    assert setting({"POLL_INTERVAL": ""}, "POLL_INTERVAL", 5) == 5
    assert setting({"POLL_INTERVAL": "12"}, "POLL_INTERVAL", 5) == 12
    for bad in ("0", "-3", "ten", "1.5"):
        with pytest.raises(SystemExit, match=repr(bad).replace(".", r"\.")):
            setting({"POLL_INTERVAL": bad}, "POLL_INTERVAL", 5)


def test_load_config_lets_the_environment_win() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.txt"
        # Notepad can save with a byte order mark, which mustn't end up in the first key.
        path.write_text(
            "LABEL_NAME=Texts\n# PHONE_TO=+1\nPOLL_INTERVAL = 9\nAI_MODEL=m\n", encoding="utf-8-sig"
        )
        with patch.dict(os.environ, {"POLL_INTERVAL": "30", "BRAVE_API_KEY": "b"}, clear=True):
            config = load_config(str(path))
    assert config == {
        "LABEL_NAME": "Texts",
        "POLL_INTERVAL": "30",
        "AI_MODEL": "m",
        "BRAVE_API_KEY": "b",
    }


def test_allowed_senders_reads_every_kind() -> None:
    assert allowed_senders({}) == set()
    raw = " Me@Example.com , @txt.bell.ca ,, +1 (604) 555-1234, inreach.garmin.com"
    parsed = allowed_senders({"ALLOWED_SENDERS": raw})
    assert parsed == {"me@example.com", "txt.bell.ca", "+16045551234", "inreach.garmin.com"}


@pytest.mark.parametrize("bad", ["604-555-1234", "+1604", "+abc", "localhost", "bob@local"])
def test_sender_list_refuses_what_it_cant_read(bad: str) -> None:
    with pytest.raises(SystemExit, match=bad.replace("+", r"\+")):
        sender_list({"REPLY_BY_SMS": bad}, "REPLY_BY_SMS")


def test_phone_number() -> None:
    assert phone_number("+1 604-555-1234") == "+16045551234"
    assert phone_number("+44 20 7946 0958") == "+442079460958"
    assert phone_number("6045551234") is None  # no country code, so it's ambiguous
    assert phone_number("") is None


def test_sender_permitted_emails() -> None:
    allowed = {"me@example.com", "txt.bell.ca"}
    assert sender_permitted("me@example.com", allowed) is True
    assert sender_permitted("5551234@txt.bell.ca", allowed) is True  # domain match
    assert sender_permitted("5551234@evil.txt.bell.ca", allowed) is False
    assert sender_permitted("stranger@spam.net", allowed) is False
    assert sender_permitted("", allowed) is False
    assert sender_permitted("anyone@anywhere.org", set()) is False


def test_sender_permitted_phones_always_need_listing() -> None:
    assert sender_permitted("+16045551234", {"+16045551234"}) is True
    assert sender_permitted("+16045559999", {"+16045551234", "txt.bell.ca"}) is False
    assert sender_permitted("+16045551234", set()) is False
