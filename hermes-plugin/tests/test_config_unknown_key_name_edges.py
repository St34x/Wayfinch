# SPDX-License-Identifier: Apache-2.0
"""Edge cases of the unknown-key display rule (config check output and the gateway log).

A name is shown as written only when it is a near-miss of a schema key at the same level (at most
max(2, 20 % of that key's length) edits after lower-casing, '-' read as '_') AND key-like (1-32 characters, an ASCII
letter first, then letters, '_' and '-', at most 2 digits). Look-alike letters, invisible and bidi-control characters,
Unicode digits, non-string keys and nested unknown keys under every schema section are shown as
``<an unknown key of N characters>``; a did-you-mean hint names only a schema key; every output line stays one
printable line. Token-shaped names are built at run time.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from glasses import config as gcfg
from glasses.config_core import is_key_like, key_name

from conftest import PLUGIN_ROOT

BASE = {"host": "127.0.0.1", "port": 8765, "bridge_port": 8766}
FAKE_TOKEN = "ghp_" + "Zq7x" * 9  # fake token shape, built at run time

# (id, name): names that look like a key name to a person but are not key-like by the rule.
LOOKALIKES = [
    ("cyrillic-o", "c\u043elour"),
    ("fullwidth", "\uff43\uff4f\uff4c\uff4f\uff55\uff52"),
    ("kelvin-sign", "\u212aey"),
    ("zero-width-space", "col\u200bour"),
    ("zero-width-joiner", "col\u200dour"),
    ("byte-order-mark", "\ufeffcolour"),
    ("rtl-override", "colour\u202etxt.exe"),
    ("rtl-isolate", "\u2067colour\u2069"),
    ("trailing-line-feed", "colour\n"),
    ("nul", "col\x00our"),
    ("delete", "col\x7four"),
    ("no-break-space", "col\u00a0our"),
    ("arabic-indic-digit", "key\u0663"),
    ("superscript-two", "key\u00b2"),
    ("combining-accent", "colou\u0301r"),
    ("next-line", "colour\x85forged"),
    ("line-separator", "colour\u2028forged"),
]


def placeholder(key: object) -> str:
    n = len(str(key))
    return f"<an unknown key of {n} character{'' if n == 1 else 's'}>"


def warnings_for(extra: Mapping[Any, Any]) -> list[str]:
    result = gcfg.check({**BASE, **extra})
    assert result.errors == [], result.errors
    return list(result.warnings)


@pytest.mark.parametrize("key", [k for _, k in LOOKALIKES], ids=[i for i, _ in LOOKALIKES])
def test_lookalike_or_invisible_character_name_is_a_placeholder(key: str) -> None:
    [warning] = warnings_for({key: 1})
    assert warning.startswith(f"{placeholder(key)}: unknown key, ignored"), warning
    assert key not in warning and warning.isprintable(), repr(warning)


@pytest.mark.parametrize(
    ("key", "shown"),
    [
        ("a" * 30 + "12", True),
        ("a" * 29 + "123", False),
        ("a1b2", True),
        ("a1b2c3", False),
        ("key-9-x", True),
        ("Ab" * 16, True),
        ("Ab" * 16 + "x", False),
        ("a-", True),
        ("a_", True),
    ],
    ids=[
        "32-chars-2-digits",
        "32-chars-3-digits",
        "2-spread-digits",
        "3-spread-digits",
        "dash",
        "32-letters",
        "33-letters",
        "trailing-dash",
        "trailing-underscore",
    ],
)
def test_key_like_shape_boundaries(key: str, shown: bool) -> None:
    assert is_key_like(key) is shown
    assert key_name(key, "port") == (key if shown else placeholder(key))


@pytest.mark.parametrize(
    ("key", "hint"),
    [
        ("hostxx", "host"),
        ("hostxxx", None),
        ("ohst", "host"),
        ("client_cert_encodingxxxx", "client_cert_encoding"),
        ("client_cert_encodingxxxxx", None),
        ("Client-Cert-Encodnig", "client_cert_encoding"),
        ("lng_thrshold", "long_threshold"),
        ("lng_thrshld", None),
        ("port12", "port"),
        ("port123", None),
    ],
    ids=[
        "2-edits-of-4",
        "3-edits-of-4",
        "swap-is-1-edit",
        "4-edits-of-20",
        "5-edits-of-20",
        "case-and-dash",
        "2-edits-of-14",
        "3-edits-of-14",
        "2-digits",
        "3-digit-suffix",
    ],
)
def test_near_miss_boundaries(key: str, hint: str | None) -> None:
    [warning] = warnings_for({key: 1})
    if hint is None:
        assert warning == f"{placeholder(key)}: unknown key, ignored", warning
    elif is_key_like(key):
        assert warning == f"{key}: unknown key, ignored — did you mean {hint}?", warning
    else:
        assert warning == f"{placeholder(key)}: unknown key, ignored — did you mean {hint}?", warning


@pytest.mark.parametrize(
    "key",
    [True, False, None, 1.5, -1, b"hots", FAKE_TOKEN.encode(), (1, 2)],
    ids=["true", "false", "none", "float", "negative-int", "bytes-typo", "bytes-token", "tuple"],
)
def test_non_string_key_is_a_placeholder_without_hint(key: object) -> None:
    [warning] = warnings_for({key: 1})
    assert warning == f"{placeholder(key)}: unknown key, ignored", warning
    assert FAKE_TOKEN not in warning


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="known gap: an integer key of more than 4300 decimal digits (YAML hex) makes str() raise ValueError",
)
def test_huge_integer_key_is_a_placeholder() -> None:
    key = int("F" * 5000, 16)
    assert key_name(key).startswith("<an unknown key of ")


@pytest.mark.parametrize(
    ("key", "known"),
    [
        ("hots\n", "host"),
        ("h\u043est", "host"),
        ("port\u200b", "port"),
        ("lanes\u202e", "lanes"),
        ("port@" + FAKE_TOKEN, "port"),
    ],
    ids=["line-feed", "cyrillic-o", "zero-width", "rtl-override", "token-suffix"],
)
def test_did_you_mean_hint_never_echoes_a_name_that_is_not_key_like(key: str, known: str) -> None:
    [warning] = warnings_for({key: 1})
    assert warning in (
        f"{placeholder(key)}: unknown key, ignored",
        f"{placeholder(key)}: unknown key, ignored — did you mean {known}?",
    ), repr(warning)
    assert key not in warning and FAKE_TOKEN not in warning


def _sections(node: Mapping[str, Any], prefix: str = "") -> Iterator[str]:
    for name, child in node.get("properties", {}).items():
        if "properties" in child:
            yield f"{prefix}{name}"
            yield from _sections(child, f"{prefix}{name}.")


def _nest(path: str, leaf: dict[Any, Any]) -> dict[str, Any]:
    out: dict[str, Any] = leaf
    for part in reversed(path.split(".")):
        out = {part: out}
    return out


@pytest.mark.parametrize("section", sorted(_sections(gcfg.schema())))
def test_nested_unknown_key_under_every_section_is_a_placeholder(section: str) -> None:
    bad = {FAKE_TOKEN: 1, "col\u200bour": 2, 7: 3}
    warnings = gcfg.check({**BASE, **_nest(section, dict(bad))}).warnings
    unknown = [w for w in warnings if "unknown key" in w]
    assert sorted(unknown) == sorted(f"{section}.{placeholder(k)}: unknown key, ignored" for k in bad), warnings
    assert all(FAKE_TOKEN not in w and "\u200b" not in w for w in warnings), warnings


def test_gateway_log_shows_placeholders_for_lookalike_and_token_names(caplog: pytest.LogCaptureFixture) -> None:
    extra = {**BASE, **{k: 1 for _, k in LOOKALIKES}, FAKE_TOKEN: 2, "signer": {"user": "", "e\u200bdge": 3}}
    with caplog.at_level(logging.DEBUG):
        assert gcfg.validate_config({"extra": extra}) is True
    messages = [r.getMessage() for r in caplog.records]
    assert FAKE_TOKEN not in caplog.text and "\u200b" not in caplog.text and "\u202e" not in caplog.text
    for _, key in LOOKALIKES:
        assert any(f"glasses.{placeholder(key)}: unknown key" in m for m in messages), (key, messages)
    assert any("glasses.signer.<an unknown key of 5 characters>: unknown key" in m for m in messages), messages
    assert all("\n" not in m and "\r" not in m for m in messages), messages


def test_config_check_cli_output_names_only_key_like_names(tmp_path: Path) -> None:
    url_key, lookalike = "wss://u:pw@h.example.com/ws", "c\u043elour"
    extra: dict[str, Any] = {**BASE, "hots": "127.0.0.1", FAKE_TOKEN: 1, url_key: 2, lookalike: 3}
    extra["signer"] = {"user": "", FAKE_TOKEN + "x": 4}
    config = tmp_path / "config.yaml"
    data = {"gateway": {"platforms": {"glasses": {"enabled": True, "extra": extra}}}}
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "glasses.admin", "config", "check", "--config", str(config)],
        cwd=PLUGIN_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    *lines, summary = proc.stdout.splitlines()
    assert sorted(lines) == sorted([
        "warning: glasses.hots: unknown key, ignored — did you mean host?",
        f"warning: glasses.{placeholder(FAKE_TOKEN)}: unknown key, ignored",
        f"warning: glasses.{placeholder(url_key)}: unknown key, ignored",
        f"warning: glasses.{placeholder(lookalike)}: unknown key, ignored",
        f"warning: glasses.signer.{placeholder(FAKE_TOKEN + 'x')}: unknown key, ignored",
    ]), proc.stdout
    assert summary == "config check: OK (0 error(s), 5 warning(s))", proc.stdout
    assert FAKE_TOKEN not in proc.stdout + proc.stderr and "pw@" not in proc.stdout and lookalike not in proc.stdout
