# SPDX-License-Identifier: Apache-2.0
"""How often config check would print a pasted secret used as a key name, and how often a typo still gets its hint.

Leak rate: share of random letter+digit tokens (``[A-Za-z0-9]``, deterministic seed) whose name an unknown-key
warning would print at ANY schema level. Before = the shape rule alone (``is_key_like``: 1-32 characters, a letter
first, at most 2 digits); after = the current rule (a key-like near-miss of a schema key at that level, see
``glasses.config_core.near_miss``). Hint rate: share of realistic typos of real keys whose warning names the typo
and hints the intended key; before = ``difflib.get_close_matches`` (cutoff 0.6) with the shape rule.

Print the table (from the repository root):
``.local/venv-dev/bin/python -m pytest hermes-plugin/tests/test_config_unknown_key_leak_rate.py -q -rP``
"""

from __future__ import annotations

import difflib
import random
import string
import sys
from collections.abc import Iterator, Mapping
from functools import cache
from typing import Any

import pytest

from glasses import config as gcfg
from glasses.config_core import edit_distance, is_key_like, near_miss

SEED = 20261010
COUNT = 100_000
LENGTHS = (16, 20, 24, 32, 40)
ALPHABET = string.ascii_letters + string.digits

# (level prefix, typo, intended schema key): one-to-two-keystroke slips, case and '-'/'_' mix-ups, missing or
# doubled separators, plurals.
TYPOS: tuple[tuple[str, str, str], ...] = tuple(
    (prefix, typo, key)
    for prefix, key, typos in (
        ("", "host", "hots hsot hostt Host HOST hst"),
        ("", "port", "prot por portt Port pport"),
        ("", "bridge_port", "bridgeport bridge-port brige_port bridge_prot Bridge_Port bridge_ports"),
        ("", "state_dir", "statedir state-dir state_dri stat_dir state_dirs"),
        ("", "public_url", "publicurl public-url public_ulr pubilc_url public_uri"),
        ("", "lanes", "lane lanse Lanes laness"),
        ("", "home_lane", "homelane home-lane home_lanes hmoe_lane home_lame"),
        ("", "client_cert_header", "client_cert_headr client-cert-header clientcertheader client_cert_headers"),
        (
            "",
            "client_cert_encoding",
            "client_cert_encodng client-cert-encoding client_cert_enconding clientcertencoding",
        ),
        ("", "long_threshold", "long_treshold long-threshold longthreshold long_threshhold long_thresold"),
        ("", "long_answers", "long_answer longanswers long-answers long_anwsers"),
        ("", "signer", "singer signr Signer signers"),
        ("", "meeting", "meetings meting meeitng Meeting"),
        ("long_answers.", "platform", "platfrom plattform Platform platforms"),
        ("signer.", "user", "uesr users User"),
        ("signer.", "public_dir", "publicdir public-dir public_dri public_dirs"),
        ("meeting.", "enabled", "enable enabeld Enabled"),
        ("meeting.", "endpoint", "endpiont end_point endpoints"),
        ("meeting.", "language_hints", "languagehints language-hints language_hint langauge_hints"),
        ("meeting.", "live_notes", "livenotes live_note live-notes"),
        ("meeting.", "max_minutes", "max_minute max-minutes maxminutes max_mintues"),
        ("meeting.", "provider", "provder providers Provider"),
        ("meeting.", "transcript_days", "transcript_day transcriptdays transcript-days"),
        ("meeting.", "daily_hours", "daily_hour dailyhours daily-hours"),
        ("meeting.", "llm", "lmm LLM llms"),
        ("meeting.llm.", "model", "modle models Model"),
        ("meeting.llm.", "provider", "provier provide Providers"),
    )
    for typo in typos.split()
)
# Word-level mistakes (a dropped word, a synonym): not near-misses by design; reported, not asserted.
WORD_LEVEL: tuple[tuple[str, str, str], ...] = (
    ("", "threshold", "long_threshold"),
    ("", "cert_header", "client_cert_header"),
    ("", "client_certificate_encoding", "client_cert_encoding"),
    ("", "url", "public_url"),
    ("", "home", "home_lane"),
    ("signer.", "public_directory", "public_dir"),
    ("meeting.", "hints", "language_hints"),
    ("meeting.", "max_duration", "max_minutes"),
)


def _levels(node: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, tuple[str, ...]]]:
    props = node.get("properties", {})
    yield prefix, tuple(props)
    for name, child in props.items():
        if "properties" in child:
            yield from _levels(child, f"{prefix}{name}.")


LEVELS = dict(_levels(gcfg.schema()))


def tokens(length: int, count: int = COUNT) -> list[str]:
    rng = random.Random(SEED * 100 + length)  # noqa: S311 - a reproducible sample, not a secret
    return ["".join(rng.choices(ALPHABET, k=length)) for _ in range(count)]


def shown_now(name: str) -> bool:
    """True when some level's warning would print *name* (key-like and a near-miss of a key at that level)."""
    return is_key_like(name) and any(near_miss(name, keys) is not None for keys in LEVELS.values())


@cache
def leak_counts(length: int) -> tuple[int, int]:
    """(shown by the shape rule alone, shown by the current rule) among ``COUNT`` random tokens of *length*."""
    sample = tokens(length)
    return sum(map(is_key_like, sample)), sum(map(shown_now, sample))


def hinted_now(prefix: str, typo: str, key: str) -> bool:
    return is_key_like(typo) and near_miss(typo, LEVELS[prefix]) == key


def hinted_before(prefix: str, typo: str, key: str) -> bool:
    return is_key_like(typo) and difflib.get_close_matches(typo, list(LEVELS[prefix]), n=1) == [key]


def _share(cases: tuple[tuple[str, str, str], ...], rule: Any) -> str:
    hits = sum(rule(*case) for case in cases)
    return f"{hits}/{len(cases)} ({hits / len(cases):.1%})"


def report() -> str:
    lines = [f"leak rate over {COUNT} random [A-Za-z0-9] tokens per length (seed {SEED}): before -> after"]
    for length in LENGTHS:
        before, after = leak_counts(length)
        lines.append(f"  {length:2d} chars: {before / COUNT:.3%} ({before}) -> {after / COUNT:.3%} ({after})")
    for label, cases in (("realistic typos", TYPOS), ("word-level mistakes", WORD_LEVEL)):
        lines.append(f"hint rate, {label}: before {_share(cases, hinted_before)} -> after {_share(cases, hinted_now)}")
    missed = [typo for prefix, typo, key in TYPOS if not hinted_now(prefix, typo, key)]
    lines.append(f"typos without the right hint now: {missed}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("length", LENGTHS)
def test_no_random_token_is_printed(length: int) -> None:
    before, after = leak_counts(length)
    assert after == 0, (length, after)
    assert before > 0 or length > 32, "the sample must contain names the shape rule alone would print"


def test_realistic_typos_keep_their_hint() -> None:
    missed = [(prefix + typo, key) for prefix, typo, key in TYPOS if not hinted_now(prefix, typo, key)]
    assert missed == []


def test_typo_warning_names_the_typo_and_the_key() -> None:
    warnings = gcfg.check({"host": "127.0.0.1", "signer": {"uesr": "x"}, "meeting": {"llm": {"modle": "x"}}}).warnings
    assert warnings == [
        "signer.uesr: unknown key, ignored — did you mean signer.user?",
        "meeting.llm.modle: unknown key, ignored — did you mean meeting.llm.model?",
    ]


@pytest.mark.parametrize(
    ("a", "b", "limit", "expected"),
    [
        ("host", "host", 2, 0),
        ("hots", "host", 2, 1),
        ("ohst", "host", 2, 1),
        ("hst", "host", 2, 1),
        ("hostxx", "host", 2, 2),
        ("hostxxx", "host", 2, 3),
        ("abcdefgh", "zyxwvuts", 3, 4),
        ("", "abc", 3, 3),
        ("", "abcd", 3, 4),
        ("ab", "ba", 0, 1),
    ],
)
def test_edit_distance_is_capped_at_limit_plus_one(a: str, b: str, limit: int, expected: int) -> None:
    assert edit_distance(a, b, limit) == expected
    assert edit_distance(b, a, limit) == expected


def test_report_prints_the_measurement() -> None:
    sys.stdout.write(report())
