# SPDX-License-Identifier: Apache-2.0
"""QA edge cases (T1.152b) of the near-miss rule for unknown key names, beyond the developer's tests.

* the near-miss window at EVERY schema key of EVERY level (``len + limit`` shown, ``len + limit + 1`` hidden), so
  the longest name any level can print is pinned (24 characters at the top level);
* the accepted residual: a short name within 2 edits of a short key (``pork`` for ``port``, ``l`` for ``llm``) is
  printed; seeded random letter+digit tokens of 7-15 characters never are (the measurement covers 16+);
* a key in the wrong section is not named and gets no hint from another level;
* a name that folds to a schema key through Unicode lower-casing (Kelvin sign) is hinted but never echoed;
* ties go to the alphabetically first key; a '-' costs no edit (three dashes + 4 extra letters still match);
* an unknown section is not descended into; a token-named key inside a list item is not echoed;
* ``edit_distance`` (with its early exit) equals a full-matrix OSA reference on a seeded sample;
* the real CLI prints no escape, carriage-return or bell character for terminal-control key names.
"""

from __future__ import annotations

import random
import string
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from glasses import config as gcfg
from glasses.config_core import edit_distance, is_key_like, near_miss

from conftest import PLUGIN_ROOT

BASE = {"host": "127.0.0.1", "port": 8765, "bridge_port": 8766}
FAKE_TOKEN = "ghp_" + "Qa7z" * 9  # fake token shape, built at run time


def placeholder(key: object) -> str:
    n = len(str(key))
    return f"<an unknown key of {n} character{'' if n == 1 else 's'}>"


def _levels(node: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, tuple[str, ...]]]:
    props = node.get("properties", {})
    yield prefix, tuple(props)
    for name, child in props.items():
        if "properties" in child:
            yield from _levels(child, f"{prefix}{name}.")


LEVELS = dict(_levels(gcfg.schema()))


def _limit(key: str) -> int:
    return max(2, len(key) // 5)


def _nest(prefix: str, leaf: dict[Any, Any]) -> dict[str, Any]:
    out: dict[str, Any] = leaf
    for part in reversed(prefix.rstrip(".").split(".")) if prefix else ():
        out = {part: out}
    return out


def _unknown_warnings(prefix: str, leaf: dict[Any, Any]) -> list[str]:
    result = gcfg.check({**BASE, **_nest(prefix, leaf)})
    assert result.errors == [], result.errors
    return [w for w in result.warnings if "unknown key" in w]


WINDOW = [(prefix, key) for prefix, keys in sorted(LEVELS.items()) for key in sorted(keys)]


@pytest.mark.parametrize(("prefix", "key"), WINDOW, ids=[f"{p}{k}" for p, k in WINDOW])
def test_near_miss_window_at_every_schema_key(prefix: str, key: str) -> None:
    inside, outside = key + "q" * _limit(key), key + "q" * (_limit(key) + 1)
    assert _unknown_warnings(prefix, {inside: 1}) == [
        f"{prefix}{inside}: unknown key, ignored — did you mean {prefix}{key}?"
    ]
    assert _unknown_warnings(prefix, {outside: 1}) == [f"{prefix}{placeholder(outside)}: unknown key, ignored"]


def test_longest_printable_name_per_level() -> None:
    longest = {prefix: max(len(k) + _limit(k) for k in keys) for prefix, keys in LEVELS.items()}
    assert longest == {"": 24, "long_answers.": 10, "signer.": 12, "meeting.": 18, "meeting.llm.": 10}


@pytest.mark.parametrize(
    ("prefix", "name", "hint"),
    [
        ("", "pork", "port"),
        ("", "p0rt", "port"),
        ("", "ho5t", "host"),
        ("meeting.", "l", "llm"),
        ("signer.", "usr", "user"),
    ],
)
def test_accepted_residual_short_name_close_to_a_short_key_is_printed(prefix: str, name: str, hint: str) -> None:
    # Residual pinned on purpose: a name of at most 6 characters within 2 edits of a short key is printed (too short
    # to be a credential; random 7+ character tokens never are). Changing this must be a decision, not a side effect.
    assert _unknown_warnings(prefix, {name: 1}) == [
        f"{prefix}{name}: unknown key, ignored — did you mean {prefix}{hint}?"
    ]


@pytest.mark.parametrize("length", range(7, 16))
def test_no_random_token_of_7_to_15_characters_is_printed(length: int) -> None:
    rng = random.Random(1520 + length)  # noqa: S311 - a reproducible sample, not a secret
    alphabet = string.ascii_letters + string.digits
    sample = ["".join(rng.choices(alphabet, k=length)) for _ in range(3000)]
    shown = [t for t in sample if is_key_like(t) and any(near_miss(t, keys) for keys in LEVELS.values())]
    assert shown == []


@pytest.mark.parametrize(
    ("prefix", "name"),
    [
        ("signer.", "port"),
        ("signer.", "hots"),
        ("meeting.", "host"),
        ("meeting.llm.", "enabled"),
        ("long_answers.", "socket"),
        ("", "socket"),
        ("", "model"),
        ("", "language_hints"),
    ],
)
def test_key_in_the_wrong_section_is_not_named_and_not_hinted_from_another_level(prefix: str, name: str) -> None:
    assert _unknown_warnings(prefix, {name: 1}) == [f"{prefix}{placeholder(name)}: unknown key, ignored"]


def test_name_that_folds_to_a_schema_key_via_unicode_is_hinted_but_not_echoed() -> None:
    kelvin = "soc\u212aet"  # KELVIN SIGN lower-cases to 'k'
    assert _unknown_warnings("signer.", {"user": "", kelvin: 1}) == [
        "signer.<an unknown key of 6 characters>: unknown key, ignored — did you mean signer.socket?"
    ]


def test_tie_goes_to_the_alphabetically_first_key() -> None:
    assert edit_distance("hort", "host", 2) == edit_distance("hort", "port", 2) == 1
    assert _unknown_warnings("", {"hort": 1}) == ["hort: unknown key, ignored — did you mean host?"]


def test_dashes_cost_no_edit() -> None:
    # 3 dashes + 4 extra letters: within 4 edits of client_cert_encoding only if '-' reads as '_'.
    name = "Client-Cert-Encodingqqqq"
    assert _unknown_warnings("", {name: 1}) == [f"{name}: unknown key, ignored — did you mean client_cert_encoding?"]


def test_unknown_section_is_not_descended_into() -> None:
    warnings = gcfg.check({**BASE, "Meeting": {FAKE_TOKEN: 1, "enabled": True}}).warnings
    assert warnings == ["Meeting: unknown key, ignored — did you mean meeting?"]


def test_token_named_key_inside_a_list_item_is_not_echoed() -> None:
    result = gcfg.check({
        **BASE,
        "lanes": ["assistant", {FAKE_TOKEN: 1}],
        "meeting": {"language_hints": [{FAKE_TOKEN: 1}]},
    })
    assert result.errors and FAKE_TOKEN not in repr(result), result


def _osa_full(a: str, b: str) -> int:
    """Reference: optimal string alignment distance over the full matrix, no early exit."""
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def test_edit_distance_matches_a_full_matrix_reference() -> None:
    rng = random.Random(152)  # noqa: S311 - a reproducible sample, not a secret
    for _ in range(20_000):
        a = "".join(rng.choices("ab_", k=rng.randint(0, 7)))
        b = "".join(rng.choices("ab_", k=rng.randint(0, 7)))
        limit = rng.randint(0, 4)
        assert edit_distance(a, b, limit) == min(_osa_full(a, b), limit + 1), (a, b, limit)


def test_edit_distance_on_a_huge_name_is_capped() -> None:
    assert edit_distance("y" * 100_000, "port", 2) == 3
    assert near_miss("port" + "q" * 1_000_000, ["port"]) is None


def test_config_check_cli_prints_no_terminal_control_character(tmp_path: Path) -> None:
    names = ["\x1b]0;pwned\x07", "port\x1b[2K\rFAKE OK", "\x1b[31mhost", "hots\x9b2J", "\u212aind"]
    extra: dict[str, Any] = {**BASE, **{n: 1 for n in names}, "signer": {"user": "", "\x1b[0muser": 1}}
    config = tmp_path / "config.yaml"
    data = {"gateway": {"platforms": {"glasses": {"enabled": True, "extra": extra}}}}
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
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
    out = proc.stdout + proc.stderr
    assert not any(c in out for c in "\x1b\r\x07\x9b"), repr(out)
    assert all(line.isprintable() for line in out.splitlines()), repr(out)
    assert "pwned" not in out and "FAKE OK" not in out, out
    assert "warning: glasses.signer.<an unknown key of 8 characters>: unknown key, ignored" in proc.stdout, proc.stdout
