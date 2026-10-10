# SPDX-License-Identifier: Apache-2.0
"""Edges of the sending side and the text helpers: unknown types, extra fields, integer spellings, byte limits at
exactly the limit and one byte either side with multi-byte characters at the edge, empty strings, and fixtures sent
in the wrong direction."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses.envelope import FrameRejected, decode
from glasses.envelope_encode import EncodeRefused, encode
from glasses.envelope_text import BIDI_CONTROLS, REPLACEMENT, cut_utf8, sanitize_card_text

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
FIXTURES = PROTOCOL / "fixtures"
INDEX: dict[str, list[dict[str, Any]]] = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))
TEXT_LIMIT = 2048  # msg.out.text without longRef, protocol/README.md limits table


def msg_out(**body: object) -> dict[str, Any]:
    frame: dict[str, Any] = json.loads((FIXTURES / "valid" / "msg.out.json").read_text(encoding="utf-8"))
    frame["body"].update(body)
    return frame


def reason(frame: object) -> str:
    with pytest.raises(EncodeRefused) as caught:
        encode(frame, VALIDATOR)
    return caught.value.reason


# --- encode: frames the schema does not know -------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda f: f.update(type="msg.unknown"), id="unknown-type"),
        pytest.param(lambda f: f.update(type=""), id="empty-type"),
        pytest.param(lambda f: f.update(type=None), id="null-type"),
        pytest.param(lambda f: f.pop("type"), id="missing-type"),
        pytest.param(lambda f: f.update(extra=1), id="extra-top-level-field"),
        pytest.param(lambda f: f["body"].update(extra="x"), id="extra-body-field"),
        pytest.param(lambda f: f.update(v=2), id="unknown-version"),
        pytest.param(lambda f: f.update(seq=True), id="boolean-seq"),
        pytest.param(lambda f: f.update(seq=-1), id="negative-seq"),
        pytest.param(lambda f: f.update(body=[]), id="body-not-object"),
        pytest.param(lambda f: f.update(id=""), id="empty-id"),
        pytest.param(lambda f: f.update(ts=""), id="empty-ts"),
    ],
)
def test_frame_outside_the_schema_is_refused_by_the_schema(change: Any) -> None:
    frame = msg_out()
    change(frame)
    assert reason(frame) == "schema"


@pytest.mark.parametrize("field", ["seq", "v"])
def test_integer_field_spelled_as_a_float_is_refused(field: str) -> None:
    frame = msg_out()
    frame[field] = float(frame[field])  # 7.0 is an integer to the schema, but the wire would carry "7.0"
    assert reason(frame) == "not_integer"


def test_encode_does_not_change_the_producers_frame() -> None:
    frame = msg_out(text="Жизнь 😀")
    before = copy.deepcopy(frame)
    encode(frame, VALIDATOR)
    assert frame == before


def test_encoded_text_is_compact_utf8_and_reads_back_as_the_frame() -> None:
    frame = msg_out(text="Ж😀\u2028")
    text = encode(frame, VALIDATOR)
    assert "Ж😀" in text  # not \\u-escaped: the size the codec measured is the size sent
    assert ", " not in text and ": " not in text
    assert json.loads(text) == frame


# --- encode: byte limit at exactly the limit and one byte either side ------------------------------------------


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        pytest.param("a" * (TEXT_LIMIT - 1), None, id="ascii-limit-minus-1"),
        pytest.param("a" * TEXT_LIMIT, None, id="ascii-at-limit"),
        pytest.param("a" * (TEXT_LIMIT + 1), "schema", id="ascii-limit-plus-1"),  # maxLength counts code points
        pytest.param("aaa" + "😀" * 511, None, id="emoji-ends-1-byte-under"),
        pytest.param("a" + "😀" * 512, "too_many_bytes", id="emoji-crosses-limit-by-1"),
        pytest.param("a" + "Ж" * 1023 + "a", None, id="cyrillic-at-limit"),
        pytest.param("a" + "Ж" * 1024, "too_many_bytes", id="cyrillic-over-by-1"),
        pytest.param("€" * 682 + "aa", None, id="three-byte-at-limit"),
        pytest.param("€" * 683, "too_many_bytes", id="three-byte-over-by-1"),
    ],
)
def test_text_byte_limit_edges(text: str, refusal: str | None) -> None:
    frame = msg_out(text=text)
    if refusal is None:
        assert json.loads(encode(frame, VALIDATOR)) == frame
    else:
        assert reason(frame) == refusal


@pytest.mark.parametrize("text", ["a" * (TEXT_LIMIT + 1), "Ж" * 1025, "😀" * 513, "€" * 683])
def test_cut_text_always_encodes(text: str) -> None:
    frame = msg_out(text=cut_utf8(text, TEXT_LIMIT))
    assert json.loads(encode(frame, VALIDATOR)) == frame


def test_sanitized_untrusted_text_encodes_and_shows_no_bidi_control() -> None:
    text = sanitize_card_text("\u202e" + "\u061c" * 700 + "😀" * 600, TEXT_LIMIT)
    sent = json.loads(encode(msg_out(text=text), VALIDATOR))
    assert not set(sent["body"]["text"]) & BIDI_CONTROLS


# --- cut_utf8 ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("max_bytes", [0, 1, 100])
def test_cut_utf8_of_an_empty_string_is_empty(max_bytes: int) -> None:
    assert cut_utf8("", max_bytes) == ""


@pytest.mark.parametrize(
    ("max_bytes", "expected"),
    [(0, ""), (1, "a"), (2, "a"), (3, "aЖ"), (4, "aЖ"), (5, "aЖ"), (6, "aЖ"), (7, "aЖ😀"), (8, "aЖ😀€"[:3])]
    + [(9, "aЖ😀"), (10, "aЖ😀€"), (11, "aЖ😀€"), (1000, "aЖ😀€")],
)
def test_cut_utf8_at_every_byte_of_mixed_widths(max_bytes: int, expected: str) -> None:
    assert cut_utf8("aЖ😀€", max_bytes) == expected  # widths 1 + 2 + 4 + 3 = 10 bytes


@pytest.mark.parametrize("char", ["Ж", "€", "😀"])
def test_cut_utf8_drops_a_character_that_fits_only_partly(char: str) -> None:
    width = len(char.encode("utf-8"))
    for budget in range(width):
        assert cut_utf8(char, budget) == ""
    assert cut_utf8(char, width) == char


def test_cut_utf8_keeps_combining_marks_and_joiners_as_code_points() -> None:
    family = "👩\u200d👧"  # 4 + 3 + 4 bytes: cut by code point, a joiner may end the result
    assert cut_utf8(family, 7) == "👩\u200d"
    assert cut_utf8("e\u0301", 2) == "e"  # combining acute (2 bytes) does not fit after "e"


def test_cut_utf8_of_a_lone_surrogate_at_the_cut() -> None:
    assert cut_utf8("a\ud800", 3) == "a"
    assert cut_utf8("a\ud800", 4) == "a\ud800"


# --- sanitize_card_text ------------------------------------------------------------------------------------------


def test_sanitize_of_an_empty_string_is_empty() -> None:
    assert sanitize_card_text("", 10) == ""


def test_sanitize_without_bidi_controls_is_cut_utf8() -> None:
    text = "plain Ж😀 text"
    for budget in range(len(text.encode("utf-8")) + 2):
        assert sanitize_card_text(text, budget) == cut_utf8(text, budget)


def test_sanitize_counts_the_replacement_not_the_control() -> None:
    # U+061C is 2 bytes, U+FFFD is 3: the budget applies to what is shown, after replacing.
    assert sanitize_card_text("\u061c" * 10, 20) == REPLACEMENT * 6
    assert sanitize_card_text("\u061c", 2) == ""


def test_sanitize_refuses_a_negative_budget() -> None:
    with pytest.raises(ValueError, match="negative"):
        sanitize_card_text("x", -1)


# --- fixtures in the wrong direction -----------------------------------------------------------------------------


def server_frames() -> list[Any]:
    kinds = ("valid", "codec-valid", "codec-invalid")
    return [pytest.param(kind, e, id=f"{kind}/{e['file']}") for kind in kinds for e in INDEX[kind] if e["dir"] == "s2d"]


@pytest.mark.parametrize(("kind", "entry"), server_frames())
def test_a_server_frame_arriving_from_the_device_is_refused(kind: str, entry: dict[str, Any]) -> None:
    text = (FIXTURES / kind / entry["file"]).read_text(encoding="utf-8")
    with pytest.raises(FrameRejected) as caught:
        decode(text, VALIDATOR)
    assert caught.value.code == "invalid_seq"


@pytest.mark.parametrize("entry", [e for e in INDEX["valid"] if e["dir"] == "both"], ids=lambda e: e["file"])
def test_a_both_way_frame_survives_decode_then_encode(entry: dict[str, Any]) -> None:
    text = (FIXTURES / "valid" / entry["file"]).read_text(encoding="utf-8")
    assert json.loads(encode(decode(text, VALIDATOR), VALIDATOR)) == json.loads(text)


def test_output_depth_counts_from_the_named_value_not_the_raw_brackets() -> None:
    frame = json.loads((FIXTURES / "valid" / "skill.result.json").read_text(encoding="utf-8"))
    for nested, code in ((34, None), (35, "frame_too_deep"), (36, "frame_too_deep")):
        value: dict[str, Any] = {}
        inner = value
        for _ in range(nested - 3):  # envelope + body + output = 3 open brackets around the chain
            inner["a"] = {}
            inner = inner["a"]
        frame["body"]["output"] = value
        text = json.dumps(frame)
        assert text.count("{") == nested
        if code is None:
            assert decode(text, VALIDATOR)["body"]["output"] == value
        else:
            with pytest.raises(FrameRejected) as caught:
                decode(text, VALIDATOR)
            assert caught.value.code == code
