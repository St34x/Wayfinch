# SPDX-License-Identifier: Apache-2.0
"""Unknown key names in config check and the gateway log: credential-looking names are replaced, others named.

A key name containing '@' or '://' (top level, nested, any length) is shown as a placeholder in the warning, which
still appears; the did-you-mean hint names only the known key. Ordinary unknown names are still shown as written.
The strict xfail pins a known gap: a line break inside an unknown key name is printed raw, so a config file can
forge an extra output line.
"""

from __future__ import annotations

import logging

import pytest

from glasses import config as gcfg

PLACEHOLDER = "<a key containing '@' or '://'>"
SECRET = "S3cr3tValue"
BASE = {"host": "127.0.0.1", "port": 8765, "bridge_port": 8766}


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({f"wss://u:{SECRET}@h.example.com/ws": 1}, f"{PLACEHOLDER}: unknown key, ignored"),
        ({f"user:{SECRET}@host": 1}, f"{PLACEHOLDER}: unknown key, ignored"),
        ({f"https://{SECRET}" * 40: 1}, f"{PLACEHOLDER}: unknown key, ignored"),
        ({"edge": {"kind": "nginx", f"https://{SECRET}.example.com": 1}}, f"edge.{PLACEHOLDER}: unknown key, ignored"),
        ({"meeting": {"llm": {f"u:{SECRET}@x": 1}}}, f"meeting.llm.{PLACEHOLDER}: unknown key, ignored"),
        ({"port@" + SECRET: 1}, f"{PLACEHOLDER}: unknown key, ignored"),
    ],
    ids=["url", "user-pass", "very-long", "nested-edge", "nested-meeting-llm", "near-miss"],
)
def test_credential_looking_unknown_key_is_warned_with_a_placeholder(extra: dict[str, object], expected: str) -> None:
    result = gcfg.check({**BASE, **extra})
    assert result.errors == [], result.errors
    assert len(result.warnings) == 1 and result.warnings[0].startswith(expected), result.warnings
    assert SECRET not in repr(result), result


def test_did_you_mean_names_only_the_known_key() -> None:
    result = gcfg.check({**BASE, "port@x": 1})
    assert result.warnings == [f"{PLACEHOLDER}: unknown key, ignored — did you mean port?"], result.warnings


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("colour", "colour: unknown key, ignored"),
        ("hots", "hots: unknown key, ignored — did you mean host?"),
        ("user.name", "user.name: unknown key, ignored"),
    ],
)
def test_ordinary_unknown_key_is_named_as_written(key: str, expected: str) -> None:
    assert gcfg.check({**BASE, key: 1}).warnings == [expected]


def test_gateway_log_never_names_a_credential_looking_key(caplog: pytest.LogCaptureFixture) -> None:
    extra = {**BASE, f"https://u:{SECRET}@h": 1, "edge": {"kind": "nginx", f"u:{SECRET}@h": 2}, "colour": 3}
    with caplog.at_level(logging.DEBUG):
        assert gcfg.validate_config({"extra": extra}) is True
    text = caplog.text
    assert SECRET not in text, text
    assert f"glasses.{PLACEHOLDER}: unknown key" in text and f"glasses.edge.{PLACEHOLDER}: unknown key" in text, text
    assert "glasses.colour: unknown key, ignored" in text, text


@pytest.mark.xfail(strict=True, reason="known gap: a line break in an unknown key name is printed raw")
def test_unknown_key_name_with_a_line_break_cannot_forge_an_output_line() -> None:
    warnings = gcfg.check({**BASE, "a\nerror: glasses.host: forged": 1}).warnings
    assert all("\n" not in warning for warning in warnings), warnings
