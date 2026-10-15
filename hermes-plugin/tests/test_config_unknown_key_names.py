# SPDX-License-Identifier: Apache-2.0
"""Unknown key names in config check and the gateway log: only key-like near-misses of a schema key are shown.

A name is shown as written only when (a) it is a near-miss of a schema key at the same level (at most
max(2, 20 % of that key's length) edits after lower-casing and reading '-' as '_'), which also gives the
did-you-mean hint, and (b) it is key-like: 1-32 characters, an ASCII letter first, then only letters, '_' and '-',
with at most 2 digits. Every other unknown name (a pasted token, a URL, a word that resembles no key, a very long
name, one with a line break or an escape character, a non-string key) is shown as
``<an unknown key of N characters>`` in the warning, which still appears; a hint then names only the known key.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from glasses import config as gcfg
from glasses.config_core import is_key_like, key_name

SECRET = "S3cr3tValue"
BASE = {"host": "127.0.0.1", "port": 8765, "bridge_port": 8766}
# Fake token shapes, built at run time so no token-shaped literal sits in the tree.
FAKE_GHP = "ghp_" + "A1b2" * 9
FAKE_SK = "sk-" + "Ab3d" * 12


def placeholder(key: object) -> str:
    n = len(str(key))
    return f"<an unknown key of {n} character{'' if n == 1 else 's'}>"


@pytest.mark.parametrize(
    ("extra", "prefix", "key"),
    [
        ({f"wss://u:{SECRET}@h.example.com/ws": 1}, "", f"wss://u:{SECRET}@h.example.com/ws"),
        ({f"user:{SECRET}@host": 1}, "", f"user:{SECRET}@host"),
        ({f"https://{SECRET}" * 40: 1}, "", f"https://{SECRET}" * 40),
        ({"signer": {"user": "", f"https://{SECRET}.example.com": 1}}, "signer.", f"https://{SECRET}.example.com"),
        ({"meeting": {"llm": {f"u:{SECRET}@x": 1}}}, "meeting.llm.", f"u:{SECRET}@x"),
        ({"port@" + SECRET: 1}, "", "port@" + SECRET),
        ({SECRET + "9": 1}, "", SECRET + "9"),
    ],
    ids=["url", "user-pass", "very-long", "nested-signer", "nested-meeting-llm", "near-miss", "three-digits"],
)
def test_credential_looking_unknown_key_is_warned_with_a_placeholder(
    extra: dict[str, object], prefix: str, key: str
) -> None:
    result = gcfg.check({**BASE, **extra})
    assert result.errors == [], result.errors
    expected = f"{prefix}{placeholder(key)}: unknown key, ignored"
    assert len(result.warnings) == 1 and result.warnings[0].startswith(expected), result.warnings
    assert SECRET not in repr(result), result


@pytest.mark.parametrize(
    "key",
    [
        FAKE_GHP,
        FAKE_SK,
        "a" * 500,
        "a" * 33,
        "a\nerror: glasses.host: forged",
        "a\rerror: glasses.host: forged",
        "colour\x1b[2J",
        "user.name",
        "1colour",
        "_colour",
        "-colour",
        "",
        "colour ",
        "cölour",
        "colour\t",
    ],
    ids=[
        "ghp-token",
        "sk-token",
        "500-chars",
        "33-chars",
        "line-feed",
        "carriage-return",
        "escape",
        "dot",
        "leading-digit",
        "leading-underscore",
        "leading-dash",
        "empty",
        "trailing-space",
        "non-ascii-letter",
        "tab",
    ],
)
def test_name_that_is_not_key_like_is_never_printed_raw(key: str) -> None:
    warnings = gcfg.check({**BASE, key: 1}).warnings
    assert len(warnings) == 1 and warnings[0].startswith(f"{placeholder(key)}: unknown key, ignored"), warnings
    if key:
        assert key not in warnings[0], warnings


def test_non_string_key_is_shown_as_a_placeholder() -> None:
    assert key_name(1) == "<an unknown key of 1 character>"
    assert key_name(12345) == "<an unknown key of 5 characters>"


def test_did_you_mean_names_only_the_known_key() -> None:
    result = gcfg.check({**BASE, "port@x": 1})
    assert result.warnings == [f"{placeholder('port@x')}: unknown key, ignored — did you mean port?"], result.warnings


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("hots", "hots: unknown key, ignored — did you mean host?"),
        ("long-treshold", "long-treshold: unknown key, ignored — did you mean long_threshold?"),
        ("Host", "Host: unknown key, ignored — did you mean host?"),
        ("BRIDGE-PORT", "BRIDGE-PORT: unknown key, ignored — did you mean bridge_port?"),
        ("clientcertheader", "clientcertheader: unknown key, ignored — did you mean client_cert_header?"),
        ("client_cert_encodnig", "client_cert_encodnig: unknown key, ignored — did you mean client_cert_encoding?"),
        ("port2", "port2: unknown key, ignored — did you mean port?"),
    ],
)
def test_key_like_near_miss_is_named_with_a_hint(key: str, expected: str) -> None:
    assert gcfg.check({**BASE, key: 1}).warnings == [expected]


# A 20-character letter+digit token with at most 2 digits: key-like, so the shape rule alone would show it.
KEY_LIKE_TOKEN = "AbcdEfgh" * 2 + "Qr12"


@pytest.mark.parametrize("key", ["colour", "Colour", "v2_key9", "a" * 32, "x", "api_key", "threshold", KEY_LIKE_TOKEN])
def test_key_like_name_that_is_no_near_miss_is_hidden_without_hint(key: str) -> None:
    assert gcfg.check({**BASE, key: 1}).warnings == [f"{placeholder(key)}: unknown key, ignored"]


def _schema_keys(node: Mapping[str, Any]) -> Iterator[str]:
    for name, child in node.get("properties", {}).items():
        yield name
        yield from _schema_keys(child)


def test_every_schema_key_is_key_like() -> None:
    names = set(_schema_keys(gcfg.schema()))
    assert "client_cert_encoding" in names and len(names) > 10
    assert [n for n in sorted(names) if not is_key_like(n) or key_name(n, n) != n] == []


def test_gateway_log_never_names_a_credential_looking_key(caplog: pytest.LogCaptureFixture) -> None:
    url_key, nested_key = f"https://u:{SECRET}@h", f"u:{SECRET}@h"
    extra = {**BASE, url_key: 1, "signer": {"user": "", nested_key: 2}, "colour": 3, FAKE_GHP: 4, "hots": 5}
    with caplog.at_level(logging.DEBUG):
        assert gcfg.validate_config({"extra": extra}) is True
    text = caplog.text
    assert SECRET not in text and FAKE_GHP not in text and "colour" not in text, text
    assert f"glasses.{placeholder(url_key)}: unknown key" in text, text
    assert f"glasses.signer.{placeholder(nested_key)}: unknown key" in text, text
    assert f"glasses.{placeholder('colour')}: unknown key, ignored" in text, text
    assert "glasses.hots: unknown key, ignored — did you mean host?" in text, text


def test_unknown_key_name_with_a_line_break_cannot_forge_an_output_line() -> None:
    warnings = gcfg.check({**BASE, "a\nerror: glasses.host: forged": 1}).warnings
    assert all("\n" not in warning for warning in warnings), warnings
