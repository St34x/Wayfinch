# SPDX-License-Identifier: Apache-2.0
"""T1.06c: text helpers for frame producers - ``cut_utf8`` cuts on a code-point boundary within a UTF-8 byte limit,
``sanitize_card_text`` replaces bidi controls with U+FFFD and then cuts. Property tests use Hypothesis."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from glasses.envelope_text import BIDI_CONTROLS, REPLACEMENT, cut_utf8, sanitize_card_text

PROPERTY = settings(database=None, deadline=None, max_examples=300)  # no .hypothesis/ directory in the tree
# Code points of every UTF-8 length (1-4 bytes) and the bidi controls, so cuts land inside multi-byte sequences.
MIXED = st.text(alphabet=st.sampled_from([*"aZ ~", "é", "я", "€", "ह", "\U0001f600", "\U00010348", *BIDI_CONTROLS]))
BUDGET = st.integers(min_value=0, max_value=120)


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


@pytest.mark.parametrize(
    ("text", "max_bytes", "expected"),
    [
        ("", 0, ""),
        ("abc", 0, ""),
        ("abc", 3, "abc"),
        ("abc", 2, "ab"),
        ("яя", 3, "я"),  # 2-byte letters: byte 3 is inside the second one
        ("яя", 4, "яя"),
        ("€x", 2, ""),  # 3-byte sign: no room for it
        ("a\U0001f600", 4, "a"),  # 4-byte emoji: 1 + 4 > 4
        ("a\U0001f600", 5, "a\U0001f600"),
    ],
)
def test_cut_utf8_examples(text: str, max_bytes: int, expected: str) -> None:
    assert cut_utf8(text, max_bytes) == expected


def test_cut_utf8_refuses_a_negative_budget() -> None:
    with pytest.raises(ValueError, match="negative"):
        cut_utf8("abc", -1)


def test_cut_utf8_counts_a_lone_surrogate_as_three_bytes_and_keeps_it() -> None:
    assert cut_utf8("a\ud800b", 4) == "a\ud800"
    assert cut_utf8("a\ud800b", 3) == "a"


@PROPERTY
@given(text=MIXED, max_bytes=BUDGET)
def test_cut_utf8_never_splits_a_code_point_and_fits(text: str, max_bytes: int) -> None:
    cut = cut_utf8(text, max_bytes)
    assert utf8_len(cut) <= max_bytes
    assert text.startswith(cut)  # a prefix in code points: no code point is split or altered
    assert text.encode("utf-8").startswith(cut.encode("utf-8"))


@PROPERTY
@given(text=st.text(), max_bytes=BUDGET)
def test_cut_utf8_keeps_the_longest_prefix_that_fits(text: str, max_bytes: int) -> None:
    cut = cut_utf8(text, max_bytes)
    if cut != text:
        assert utf8_len(text[: len(cut) + 1]) > max_bytes  # one more code point would not fit
    else:
        assert utf8_len(text) <= max_bytes


def test_sanitize_replaces_every_bidi_control() -> None:
    controls = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    assert frozenset(controls) == BIDI_CONTROLS
    assert sanitize_card_text("rm " + controls + " ok", 1000) == "rm " + REPLACEMENT * len(controls) + " ok"


def test_sanitize_keeps_neighbours_of_the_bidi_ranges() -> None:
    neighbours = "\u061b\u061d\u200d\u2010\u2029\u202f\u2065\u206a"
    assert sanitize_card_text(neighbours, 1000) == neighbours


def test_sanitize_cuts_after_replacing() -> None:
    # U+200F is 3 bytes and U+FFFD is 3 bytes too: the cut is measured on the replaced text.
    assert sanitize_card_text("ab\u200fcd", 5) == "ab" + REPLACEMENT
    assert sanitize_card_text("ab\u200fcd", 4) == "ab"


def test_sanitize_cyrillic_command_fits_an_approval_summary() -> None:
    summary = sanitize_card_text("удалить " * 125, 300)  # 1000 characters, 2 bytes per letter
    assert utf8_len(summary) <= 300
    assert summary == ("удалить " * 125)[: len(summary)]


@PROPERTY
@given(text=MIXED, max_bytes=BUDGET)
def test_sanitize_leaves_no_bidi_control_and_fits(text: str, max_bytes: int) -> None:
    clean = sanitize_card_text(text, max_bytes)
    assert not BIDI_CONTROLS & set(clean)
    assert utf8_len(clean) <= max_bytes
    replaced = "".join(REPLACEMENT if char in BIDI_CONTROLS else char for char in text)
    assert replaced.startswith(clean)
