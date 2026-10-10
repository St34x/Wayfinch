# SPDX-License-Identifier: Apache-2.0
"""T1.06a: envelope decode core - frame caps before parsing, nesting pre-scan, JSON parse, lone surrogates, schema,
standard error frames without input echo (protocol/README.md, Limits and validation notes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from glasses import envelope, envelope_scan
from glasses.envelope import (
    FrameRejected,
    FrameTooDeep,
    FrameTooLarge,
    InvalidFrame,
    InvalidJson,
    InvalidText,
    decode,
    error_frame,
)

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
SCHEMA = json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
MARKER = "ECHO-MARKER-7f3a"
LOADS = json.loads  # the helpers keep parsing while a test replaces json.loads
HEADER = '{"v":1,"id":"01JABCDEFGHJKMNPQRSTVWXYZ0","ts":"2026-10-03T18:00:00Z",'


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = LOADS((PROTOCOL / "fixtures" / name).read_text(encoding="utf-8"))
    return loaded


def padded(frame: dict[str, Any], size: int) -> bytes:
    """``frame`` serialised and padded with JSON whitespace to exactly ``size`` UTF-8 bytes."""
    raw = json.dumps(frame).encode("utf-8")
    assert len(raw) <= size
    return raw[:-1] + b" " * (size - len(raw)) + b"}"


def nested_manifest(levels: int) -> str:
    """A schema-valid skill.manifest frame whose raw text holds ``levels`` nested open brackets.

    Envelope = 1, body = 2, skills[] = 3, the item = 4: at 35 levels the item has depth 32, the per-field maximum.
    """
    item = "{}"
    for _ in range(levels - 4):
        item = '{"a":' + item + "}"
    return HEADER + '"type":"skill.manifest","body":{"manifestVersion":1,"skills":[' + item + "]}}"


@pytest.fixture
def parser_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the JSON parser ran")

    monkeypatch.setattr(json, "loads", boom)


# ---------------------------------------------------------------------------------------- caps before parsing


@pytest.mark.parametrize("as_text", [False, True])
def test_frame_cap_boundary(as_text: bool) -> None:
    raw = padded(fixture("valid/msg.in.json"), envelope_scan.FRAME_CAP)
    frame = decode(raw.decode("utf-8") if as_text else raw, VALIDATOR)
    assert frame["type"] == "msg.in"
    assert frame["body"] == fixture("valid/msg.in.json")["body"]


@pytest.mark.parametrize("as_text", [False, True])
@pytest.mark.usefixtures("parser_must_not_run")
def test_over_cap_rejected_before_the_parser_runs(as_text: bool) -> None:
    raw = padded(fixture("valid/msg.in.json"), envelope_scan.FRAME_CAP + 1)
    with pytest.raises(FrameTooLarge):
        decode(raw.decode("utf-8") if as_text else raw, VALIDATOR)


@pytest.mark.usefixtures("parser_must_not_run")
def test_cap_counts_utf8_bytes_not_characters() -> None:
    text = "я" * (envelope_scan.FRAME_CAP // 2 + 1)  # fewer characters than the cap, more bytes
    with pytest.raises(FrameTooLarge):
        decode(text, VALIDATOR)


@pytest.mark.parametrize("name", ["valid/audio.chunk.json", "valid/meeting.audio.json"])
def test_audio_cap_boundary(name: str) -> None:
    assert decode(padded(fixture(name), envelope_scan.AUDIO_FRAME_CAP), VALIDATOR)["type"] == fixture(name)["type"]


@pytest.mark.parametrize("name", ["valid/audio.chunk.json", "valid/meeting.audio.json"])
@pytest.mark.usefixtures("parser_must_not_run")
def test_audio_over_cap_rejected_before_the_parser_runs(name: str) -> None:
    with pytest.raises(FrameTooLarge):
        decode(padded(fixture(name), envelope_scan.AUDIO_FRAME_CAP + 1), VALIDATOR)


def test_audio_cap_also_holds_when_the_type_key_is_escaped() -> None:
    raw = padded(fixture("valid/audio.chunk.json"), envelope_scan.AUDIO_FRAME_CAP + 1)
    escaped = raw.replace(b'"type"', b'"\\u0074ype"')
    assert envelope_scan.scan(escaped.decode("utf-8")).top_level_types == frozenset()
    with pytest.raises(FrameTooLarge):
        decode(escaped, VALIDATOR)


def test_non_audio_frame_between_the_caps_is_accepted() -> None:
    assert decode(padded(fixture("valid/msg.in.json"), 40_000), VALIDATOR)["type"] == "msg.in"


# ---------------------------------------------------------------------------------------- nesting pre-scan


def test_depth_35_passes_and_36_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    assert decode(nested_manifest(envelope_scan.MAX_DEPTH), VALIDATOR)["type"] == "skill.manifest"
    monkeypatch.setattr(json, "loads", lambda *_a, **_k: pytest.fail("the JSON parser ran"))
    with pytest.raises(FrameTooDeep):
        decode(nested_manifest(envelope_scan.MAX_DEPTH + 1), VALIDATOR)


@pytest.mark.parametrize(("text", "too_deep"), [("[" * 35 + "]" * 35, False), ("[" * 36 + "]" * 36, True)])
def test_scan_boundary(text: str, too_deep: bool) -> None:
    assert envelope_scan.scan(text).too_deep is too_deep


def test_scan_ignores_brackets_inside_strings() -> None:
    text = '{"a":"' + "[" * 100 + '\\"' + "{" * 100 + '"}'
    assert envelope_scan.scan(text).too_deep is False


def test_scan_closers_never_lower_depth_below_zero() -> None:
    assert envelope_scan.scan("]" * 10 + "[" * 36).too_deep is True


def test_scan_of_an_unterminated_string_is_linear() -> None:
    # Many quotes and no closing one: a scanner that retried the string grammar at every quote would be quadratic.
    text = '"' + '\\"' * (envelope_scan.FRAME_CAP // 2 - 1) + "["
    assert envelope_scan.scan(text) == envelope_scan.Scan(too_deep=False, top_level_types=frozenset())


@pytest.mark.usefixtures("parser_must_not_run")
@pytest.mark.parametrize("opener", ["[", "{"])
def test_64_kib_of_open_brackets_gives_a_clean_error(opener: str) -> None:
    with pytest.raises(FrameTooDeep):
        decode(opener * envelope_scan.FRAME_CAP, VALIDATOR)


def test_scan_reads_only_top_level_type_values() -> None:
    text = HEADER + '"type":"msg.in","body":{"type":"audio.chunk","x":["type",":","audio.chunk"]}}'
    assert envelope_scan.scan(text).top_level_types == frozenset({"msg.in"})


# Strings full of the characters a scanner could mistake for structure, keys included.
TRICKY_TEXT = st.text(alphabet=st.sampled_from([*'[]{}":,\\ab', "\u2028", "я", "\U0001f600", "\x00"]), max_size=8)
JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | TRICKY_TEXT,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(TRICKY_TEXT, inner, max_size=3),
    max_leaves=12,
)


def parsed_depth(value: object) -> int:
    if isinstance(value, dict):
        return 1 + max(map(parsed_depth, value.values()), default=0)
    if isinstance(value, list):
        return 1 + max(map(parsed_depth, value), default=0)
    return 0


@settings(database=None, deadline=None, max_examples=300)
@given(
    value=JSON_VALUES,
    wrappers=st.lists(st.booleans(), max_size=envelope_scan.MAX_DEPTH + 5),
    ascii_only=st.booleans(),
    indent=st.sampled_from([None, 0, 2]),
)
def test_scan_depth_is_never_below_the_parsed_depth(
    value: object, wrappers: list[bool], ascii_only: bool, indent: int | None
) -> None:
    for as_object in wrappers:  # push the depth across the boundary with arrays and objects mixed
        value = {"k": value} if as_object else [value]
    text = json.dumps(value, ensure_ascii=ascii_only, indent=indent)
    depth = parsed_depth(json.loads(text))
    assert envelope_scan.scan(text).too_deep is (depth > envelope_scan.MAX_DEPTH)


# ---------------------------------------------------------------------------------------- parse and text


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b'{"v":1,}',
        HEADER.encode() + b'"type":"ping","body":{"x":NaN}}',
        HEADER.encode() + b'"type":"ping","body":{"x":' + b"9" * 5000 + b"}}",
        HEADER.encode() + b'"type":"skill.result","body":{"callId":"c1","status":"ok","output":{"p":1e999}}}',
        HEADER.encode() + b'"type":"skill.result","body":{"callId":"c1","status":"ok","output":{"p":-1e400}}}',
    ],
    ids=["empty", "truncated", "trailing-comma", "nan", "huge-int", "overflow-float", "negative-overflow-float"],
)
def test_invalid_json_is_rejected(raw: bytes) -> None:
    with pytest.raises(InvalidJson):
        decode(raw, VALIDATOR)


def test_finite_floats_in_an_open_object_are_accepted() -> None:
    raw = HEADER + '"type":"skill.result","body":{"callId":"c1","status":"ok","output":{"p":1e308,"q":-2.5e-300}}}'
    assert decode(raw, VALIDATOR)["body"] == {"callId": "c1", "status": "ok", "output": {"p": 1e308, "q": -2.5e-300}}


def test_huge_text_is_refused_before_it_is_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envelope, "utf8_size", lambda _raw: pytest.fail("the str was encoded"))
    with pytest.raises(FrameTooLarge):
        decode("a" * (envelope_scan.FRAME_CAP + 1), VALIDATOR)


def test_lone_surrogate_escape_is_rejected() -> None:
    raw = (PROTOCOL / "fixtures/codec-invalid/d2s-msg-in-lone-surrogate.json").read_bytes()
    with pytest.raises(InvalidText):
        decode(raw, VALIDATOR)


def test_lone_surrogate_in_an_object_key_is_rejected() -> None:
    raw = nested_manifest(4).replace('"skills":[{}]', '"skills":[{"k\\udc00":[1,{"x":"y"}]}]')
    with pytest.raises(InvalidText):
        decode(raw, VALIDATOR)


def test_raw_lone_surrogate_in_text_input_is_rejected() -> None:
    raw = json.dumps(fixture("valid/msg.in.json"), ensure_ascii=False).replace("what", "wh\ud800t")
    with pytest.raises(InvalidText):
        decode(raw, VALIDATOR)


def test_surrogate_pair_escape_is_a_scalar_value() -> None:
    frame = fixture("valid/msg.in.json")
    frame["body"]["text"] = "ok \U0001f600"
    decoded = decode(json.dumps(frame), VALIDATOR)  # ensure_ascii: the emoji travels as \ud83d\ude00
    assert decoded["body"] == frame["body"]


def test_bytes_that_are_not_utf8_are_rejected() -> None:
    with pytest.raises(InvalidText):
        decode(b'{"v":1,"x":"\xff"}', VALIDATOR)


# ---------------------------------------------------------------------------------------- schema


def test_every_valid_fixture_without_seq_decodes_to_itself() -> None:
    # Frames with seq travel S→D: decode (the server side) refuses them by direction (test_envelope_rules.py).
    for path in sorted((PROTOCOL / "fixtures" / "valid").glob("*.json")):
        frame = json.loads(path.read_text(encoding="utf-8"))
        if "seq" not in frame:
            assert decode(path.read_bytes(), VALIDATOR) == frame, path.name


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.update(extra=MARKER),
        lambda f: f["body"].update(text=MARKER, origin=MARKER),
        lambda f: f.update(type=MARKER),
        lambda f: f.update(v=2),
    ],
    ids=["unknown-field", "bad-enum", "unknown-type", "version"],
)
def test_schema_invalid_frame_is_rejected(mutate: Any) -> None:
    frame = fixture("valid/msg.in.json")
    mutate(frame)
    with pytest.raises(InvalidFrame):
        decode(json.dumps(frame), VALIDATOR)


def test_top_level_array_is_schema_invalid() -> None:
    with pytest.raises(InvalidFrame):
        decode("[1, 2]", VALIDATOR)


# ---------------------------------------------------------------------------------------- error frames


def rejected_inputs() -> list[str]:
    msg_in = fixture("valid/msg.in.json")
    msg_in["body"]["text"] = MARKER
    schema_invalid = dict(msg_in, extra=MARKER)
    return [
        json.dumps(msg_in) + " " * envelope_scan.FRAME_CAP,
        "[" * 40 + json.dumps(MARKER),
        '{"' + MARKER + '":',
        json.dumps(msg_in).replace(MARKER, MARKER + "\\ud800"),
        json.dumps(schema_invalid),
    ]


@pytest.mark.parametrize("raw", rejected_inputs(), ids=["cap", "depth", "json", "surrogate", "schema"])
def test_errors_never_echo_input(raw: str) -> None:
    with pytest.raises(FrameRejected) as caught:
        decode(raw, VALIDATOR)
    frame = error_frame(caught.value, "01JABCDEFGHJKMNPQRSTVWXYZ1", "2026-10-03T18:00:01Z")
    serialised = json.dumps(frame)
    assert MARKER not in serialised
    assert MARKER not in str(caught.value)
    assert MARKER not in repr(caught.value.args)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None  # no chained parser/decoder error that holds the payload
    assert VALIDATOR.is_valid(frame), serialised
    assert frame["body"] == {"code": caught.value.code, "message": caught.value.message}
    assert "seq" not in frame


def test_error_codes_are_distinct_and_well_formed() -> None:
    kinds = [FrameTooLarge, FrameTooDeep, InvalidJson, InvalidText, InvalidFrame]
    codes = [kind.code for kind in kinds]
    assert len(set(codes)) == len(codes)
    for kind in kinds:
        assert issubclass(kind, FrameRejected)
        assert VALIDATOR.is_valid(error_frame(kind(), "01JABCDEFGHJKMNPQRSTVWXYZ1", "2026-10-03T18:00:01Z"))
