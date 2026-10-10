# SPDX-License-Identifier: Apache-2.0
"""T1.06d: every golden fixture of protocol/fixtures (T1.03) through the server codec.

A frame from the device (``d2s``) goes through ``decode`` (the server's receiving side), a frame the server sends
(``s2d``) through ``encode``; a ``both`` frame through each. codec-valid/ frames are accepted, codec-invalid/ frames
are refused with the error the rule they break maps to; valid/ frames pass, invalid/ frames fail the schema. A
valid ``d2s`` frame of a device-only type through ``encode`` is refused by direction: the server never sends it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses.envelope import FrameRejected, decode
from glasses.envelope_encode import EncodeRefused, encode
from glasses.envelope_rules import D2S_ONLY

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
FIXTURES = PROTOCOL / "fixtures"
INDEX: dict[str, list[dict[str, Any]]] = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))

# The rule a codec-invalid fixture breaks -> (decode error code, encode refusal reason).
REFUSALS = {
    "bytes": ("field_too_long", "too_many_bytes"),
    "depth": ("frame_too_deep", "too_deep"),
    "seq": ("invalid_seq", "seq"),
    "ts": ("invalid_frame", "feb_29"),
    "surrogate": ("invalid_text", "invalid_text"),
}


def raw(kind: str, entry: dict[str, Any]) -> str:
    return (FIXTURES / kind / entry["file"]).read_text(encoding="utf-8")


def entries(kind: str) -> list[Any]:
    return [pytest.param(e, id=e["file"]) for e in INDEX[kind]]


def broken_rule(entry: dict[str, Any]) -> str:
    """Which ``REFUSALS`` row a codec fixture exercises, from its index metadata."""
    if "bytes" in entry:
        return "bytes"
    if "depth" in entry:
        return "depth"
    if "surrogate" in entry["rule"]:
        return "surrogate"
    return str(entry["field"])


def refusal_code(text: bytes | str) -> str:
    with pytest.raises(FrameRejected) as caught:
        decode(text, VALIDATOR)
    return caught.value.code


def refusal_reason(text: str) -> str:
    with pytest.raises(EncodeRefused) as caught:
        encode(json.loads(text), VALIDATOR)
    return caught.value.reason


@pytest.mark.parametrize("entry", entries("codec-valid"))
def test_codec_valid_fixture_is_accepted(entry: dict[str, Any]) -> None:
    text = raw("codec-valid", entry)
    frame = json.loads(text)
    if entry["dir"] == "d2s":
        assert decode(text, VALIDATOR) == frame
        assert decode(text.encode("utf-8"), VALIDATOR) == frame
        if frame["type"] in D2S_ONLY:  # the server never sends a type only the device sends
            assert refusal_reason(text) == "direction"
    else:
        assert json.loads(encode(frame, VALIDATOR)) == frame


@pytest.mark.parametrize("entry", entries("codec-invalid"))
def test_codec_invalid_fixture_is_refused_by_its_rule(entry: dict[str, Any]) -> None:
    text = raw("codec-invalid", entry)
    code, reason = REFUSALS[broken_rule(entry)]
    if entry["dir"] == "d2s":
        assert refusal_code(text) == code
        assert refusal_code(text.encode("utf-8")) == code
    else:
        assert refusal_reason(text) == reason


@pytest.mark.parametrize("entry", entries("valid"))
def test_valid_fixture_passes_its_direction(entry: dict[str, Any]) -> None:
    text = raw("valid", entry)
    frame = json.loads(text)
    if entry["dir"] in ("d2s", "both"):
        assert decode(text, VALIDATOR) == frame
    if entry["dir"] in ("s2d", "both"):
        assert json.loads(encode(frame, VALIDATOR)) == frame
    else:
        assert refusal_reason(text) == "direction"


@pytest.mark.parametrize("entry", entries("invalid"))
def test_invalid_fixture_fails_the_schema_both_ways(entry: dict[str, Any]) -> None:
    text = raw("invalid", entry)
    assert refusal_code(text) == "invalid_frame"
    assert refusal_reason(text) == "schema"


def test_every_codec_fixture_has_a_direction_the_codec_handles() -> None:
    codec = INDEX["codec-valid"] + INDEX["codec-invalid"]
    assert codec
    assert {e["dir"] for e in codec} <= {"d2s", "s2d"}
    assert {broken_rule(e) for e in INDEX["codec-invalid"]} <= set(REFUSALS)
