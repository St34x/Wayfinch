# SPDX-License-Identifier: Apache-2.0
"""Edges of envelope decode: payload framing (BOM, whitespace, trailing data), UTF-8 byte classes, exact caps with
4-byte characters, the pre-scan's string grammar, lone surrogates in every position, ``seq`` and Feb 29 on every
device frame type (protocol/README.md, Delivery and Limits and validation notes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses import envelope_scan
from glasses.envelope import (
    FrameRejected,
    FrameTooDeep,
    FrameTooLarge,
    InvalidFrame,
    InvalidJson,
    InvalidText,
    decode,
)
from glasses.envelope_rules import D2S_ONLY

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))
LOADS = json.loads  # kept while a test replaces json.loads
EMOJI = "\U0001f600"  # 4 UTF-8 bytes, a surrogate pair when escaped


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = LOADS((PROTOCOL / "fixtures" / name).read_text(encoding="utf-8"))
    return loaded


def msg_in_raw() -> str:
    return (PROTOCOL / "fixtures/valid/msg.in.json").read_text(encoding="utf-8").strip()


@pytest.fixture
def parser_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the JSON parser ran")

    monkeypatch.setattr(json, "loads", boom)


# ---------------------------------------------------------------------------------------- payload framing


@pytest.mark.parametrize("as_text", [False, True])
def test_byte_order_mark_is_invalid_json(as_text: bool) -> None:
    raw = "\ufeff" + msg_in_raw()
    with pytest.raises(InvalidJson):
        decode(raw if as_text else raw.encode("utf-8"), VALIDATOR)


def test_json_whitespace_around_the_frame_is_accepted() -> None:
    assert decode(" \t\r\n" + msg_in_raw() + "\n\r\t ", VALIDATOR)["type"] == "msg.in"


@pytest.mark.parametrize("suffix", ["x", "{}", " " + "{}", "\x00", "\u00a0"])
def test_data_after_the_frame_is_invalid_json(suffix: str) -> None:
    with pytest.raises(InvalidJson):
        decode(msg_in_raw() + suffix, VALIDATOR)


def test_two_frames_in_one_payload_are_invalid_json() -> None:
    with pytest.raises(InvalidJson):
        decode(msg_in_raw() + msg_in_raw(), VALIDATOR)


@pytest.mark.parametrize(
    "bad",
    [
        b"\xc0\xaf",  # overlong encoding of "/"
        b"\xed\xa0\x80",  # an encoded UTF-16 surrogate (CESU-8)
        b"\xf4\x90\x80\x80",  # above U+10FFFF
        b"\xf8\x88\x80\x80\x80",  # a 5-byte sequence
        b"\xe2\x82",  # a truncated 3-byte sequence
        b"\x80",  # a lone continuation byte
    ],
)
def test_bytes_outside_utf8_are_invalid_text_wherever_they_sit(bad: bytes) -> None:
    raw = msg_in_raw().encode("utf-8")
    frame_text = fixture("valid/msg.in.json")["body"]["text"].encode("utf-8")
    with pytest.raises(InvalidText):
        decode(raw.replace(frame_text, frame_text + bad, 1), VALIDATOR)
    with pytest.raises(InvalidText):
        decode(raw + bad, VALIDATOR)


# ---------------------------------------------------------------------------------------- exact caps


def skill_result_of_size(size: int, filler: str) -> str:
    """A schema-valid skill.result frame of exactly ``size`` UTF-8 bytes, ``filler`` repeated in an open object."""
    frame = fixture("valid/skill.result.json")
    frame["body"]["output"] = {"blob": ""}
    base = len(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
    count, rest = divmod(size - base, len(filler.encode("utf-8")))
    frame["body"]["output"]["blob"] = filler * count + "a" * rest
    raw = json.dumps(frame, ensure_ascii=False)
    assert len(raw.encode("utf-8")) == size
    return raw


@pytest.mark.parametrize("as_text", [False, True])
def test_frame_cap_is_exact_with_4_byte_characters(as_text: bool) -> None:
    at_cap = skill_result_of_size(envelope_scan.FRAME_CAP, EMOJI)
    assert len(at_cap) < envelope_scan.FRAME_CAP  # far fewer code points than bytes
    assert decode(at_cap if as_text else at_cap.encode("utf-8"), VALIDATOR)["type"] == "skill.result"
    over = skill_result_of_size(envelope_scan.FRAME_CAP + 1, EMOJI)
    with pytest.raises(FrameTooLarge):
        decode(over if as_text else over.encode("utf-8"), VALIDATOR)


def test_frame_cap_counts_the_escaped_spelling_not_the_decoded_string() -> None:
    frame = fixture("valid/skill.result.json")
    frame["body"]["output"] = {"blob": EMOJI * 6000}  # 24 000 B decoded, 72 000 B as \\ud83d\\ude00 escapes
    with pytest.raises(FrameTooLarge):
        decode(json.dumps(frame), VALIDATOR)
    assert decode(json.dumps(frame, ensure_ascii=False), VALIDATOR)["body"] == frame["body"]


def audio_raw(size: int, *, type_spelling: str = '"audio.chunk"', type_last: bool = False) -> str:
    frame = fixture("valid/audio.chunk.json")
    if type_last:
        frame = {key: value for key, value in frame.items() if key != "type"} | {"type": frame["type"]}
    raw = json.dumps(frame).replace('"audio.chunk"', type_spelling, 1)
    return raw[:-1] + " " * (size - len(raw)) + "}"


@pytest.mark.usefixtures("parser_must_not_run")
def test_audio_type_after_the_body_is_still_refused_unparsed() -> None:
    with pytest.raises(FrameTooLarge):
        decode(audio_raw(envelope_scan.AUDIO_FRAME_CAP + 1, type_last=True), VALIDATOR)


@pytest.mark.usefixtures("parser_must_not_run")
def test_audio_type_with_whitespace_around_the_colon_is_refused_unparsed() -> None:
    raw = audio_raw(envelope_scan.AUDIO_FRAME_CAP + 1).replace('"type": ', '"type" \n:\t ', 1)
    raw = raw[:-4] + "}"  # keep the size: the colon spacing added 3 bytes
    assert len(raw.encode("utf-8")) == envelope_scan.AUDIO_FRAME_CAP + 1
    with pytest.raises(FrameTooLarge):
        decode(raw, VALIDATOR)


def test_audio_cap_holds_when_the_type_value_is_escaped() -> None:
    raw = audio_raw(envelope_scan.AUDIO_FRAME_CAP + 1, type_spelling='"audio\\u002echunk"')
    assert envelope_scan.scan(raw).top_level_types == frozenset({"audio\\u002echunk"})
    with pytest.raises(FrameTooLarge):
        decode(raw, VALIDATOR)
    at_cap = audio_raw(envelope_scan.AUDIO_FRAME_CAP, type_spelling='"audio\\u002echunk"')
    assert decode(at_cap, VALIDATOR)["type"] == "audio.chunk"


def test_a_nested_audio_type_does_not_lower_the_cap() -> None:
    frame = fixture("valid/skill.result.json")
    frame["body"]["output"] = {"type": "audio.chunk", "blob": "a" * 40_000}
    assert decode(json.dumps(frame), VALIDATOR)["body"] == frame["body"]


# ---------------------------------------------------------------------------------------- pre-scan string grammar


@pytest.mark.parametrize(
    ("text", "too_deep"),
    [
        ('"\\\\"' + "[" * 36, True),  # the string ends after an escaped backslash: the brackets are real
        ('"\\\\\\"' + "[" * 36 + '"', False),  # backslash, then an escaped quote: still inside the string
        ('{"' + "[" * 40 + '":1}', False),  # brackets in a key
        ('"\\u005b' + "[" * 36 + '"', False),  # an escaped bracket and raw ones, all in a string
        ('["\\"]"],' * 3 + "[" * 35 + "]" * 35, False),  # 35 levels after closed arrays of strings holding \\" and ]
        ('["\\"]"],' * 3 + "[" * 36 + "]" * 36, True),
    ],
)
def test_scan_depth_follows_the_json_string_grammar(text: str, too_deep: bool) -> None:
    assert envelope_scan.scan(text).too_deep is too_deep


@pytest.mark.usefixtures("parser_must_not_run")
def test_open_brackets_after_a_closed_string_with_escapes_are_refused_unparsed() -> None:
    with pytest.raises(FrameTooDeep):
        decode('{"a":"\\\\\\"[{","b":' + "[" * 40, VALIDATOR)


def test_deep_skill_result_of_only_objects_or_only_arrays_at_the_limit() -> None:
    for opener in ("object", "array"):
        value: object = 1
        for _ in range(31):
            value = {"a": value} if opener == "object" else [value]
        frame = fixture("valid/skill.result.json")
        frame["body"]["output"] = {"a": value}  # output = 1, 31 more levels = 32
        assert decode(json.dumps(frame), VALIDATOR)["body"] == frame["body"]
        frame["body"]["output"] = {"a": [value]}
        with pytest.raises(FrameTooDeep):
            decode(json.dumps(frame), VALIDATOR)


# ---------------------------------------------------------------------------------------- lone surrogates


def skill_result_with(output_json: str) -> str:
    raw = json.dumps(fixture("valid/skill.result.json"))
    start = raw.index('"output": ') + len('"output": ')
    end = raw.index("}", start) + 1
    return raw[:start] + output_json + raw[end:]


@pytest.mark.parametrize(
    "output_json",
    [
        '{"k": "\\udc00"}',  # a low surrogate alone
        '{"k": "\\udc00\\ud800"}',  # a reversed pair
        '{"k": "\\ud800\\u0041"}',  # a high surrogate before a non-surrogate escape
        '{"k": "\\ud83d"}',  # the high half of an emoji at the end of the string
        '{"k": ["ok", "\\ud800"]}',  # an array item
        '{"a": {"b": {"\\udfff": 1}}}',  # a nested key
        '{"k\\ud800x": 1}',  # inside a key
    ],
)
def test_lone_surrogate_anywhere_in_an_open_object_is_invalid_text(output_json: str) -> None:
    with pytest.raises(InvalidText):
        decode(skill_result_with(output_json), VALIDATOR)


def test_raw_surrogate_halves_in_text_input_are_refused_also_when_paired() -> None:
    # Python text holding two surrogate code units is not a decoded emoji: each half is a lone surrogate.
    with pytest.raises(InvalidText):
        decode(skill_result_with('{"k": "\ud83d\ude00"}'), VALIDATOR)


def test_noncharacters_are_scalar_values_and_accepted() -> None:
    output = {"k": "\ufffe\uffff\ufdd0\U0010ffff"}
    frame = fixture("valid/skill.result.json")
    frame["body"]["output"] = output
    assert decode(json.dumps(frame), VALIDATOR)["body"]["output"] == output


# ---------------------------------------------------------------------------------------- every device frame type


def device_fixture(frame_type: str) -> dict[str, Any]:
    return fixture(f"valid/{frame_type}.json")


@pytest.mark.parametrize("frame_type", sorted(D2S_ONLY))
def test_every_device_frame_type_decodes_and_refuses_seq(frame_type: str) -> None:
    frame = device_fixture(frame_type)
    assert decode(json.dumps(frame), VALIDATOR) == frame
    for seq in (1, 0, None, "1"):
        with pytest.raises(FrameRejected) as caught:
            decode(json.dumps(frame | {"seq": seq}), VALIDATOR)
        assert caught.value.code in {"invalid_seq", "invalid_frame"}


@pytest.mark.parametrize("frame_type", sorted(D2S_ONLY))
@pytest.mark.parametrize(
    ("year", "accepted"),
    [("1900", False), ("2000", True), ("2100", False), ("2400", True), ("2024", True), ("2023", False)],
)
def test_feb_29_in_ts_of_every_device_frame_type(frame_type: str, year: str, accepted: bool) -> None:
    frame = device_fixture(frame_type) | {"ts": f"{year}-02-29T23:59:59.5+14:00"}
    if accepted:
        assert decode(json.dumps(frame), VALIDATOR)["ts"] == frame["ts"]
    else:
        with pytest.raises(InvalidFrame):
            decode(json.dumps(frame), VALIDATOR)


@pytest.mark.parametrize("field", ["id", "ts", "type"])
@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\u2028"])
def test_a_line_break_after_an_anchored_field_is_schema_invalid(field: str, suffix: str) -> None:
    frame = fixture("valid/msg.in.json")
    frame[field] += suffix
    with pytest.raises(InvalidFrame):
        decode(json.dumps(frame), VALIDATOR)
