# SPDX-License-Identifier: Apache-2.0
"""Property tests of envelope decode (Hypothesis): on any input it returns a frame or raises one of the fixed
``FrameRejected`` kinds - never another exception, never a rejection that carries input; a JSON value of any shape
in an open object either round-trips or is refused for a rule it breaks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from glasses.envelope import (
    FieldTooLong,
    FrameRejected,
    FrameTooDeep,
    FrameTooLarge,
    InvalidFrame,
    InvalidJson,
    InvalidSeq,
    InvalidText,
    decode,
    error_frame,
)

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))
KINDS = (FrameTooLarge, FrameTooDeep, InvalidJson, InvalidText, InvalidFrame, InvalidSeq, FieldTooLong)
FIXTURES = sorted((PROTOCOL / "fixtures").glob("**/*.json"))
FIXTURE_BYTES = [path.read_bytes() for path in FIXTURES if path.name != "index.json"]
# Reproducible in CI: fixed seed, nothing written to a local example database.
PROPERTY = settings(
    max_examples=300, deadline=None, database=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow]
)

JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=40,
)


def outcome(raw: bytes | str) -> FrameRejected | None:
    """None when ``raw`` decodes; the rejection otherwise. Any other exception fails the property."""
    try:
        decode(raw, VALIDATOR)
    except FrameRejected as rejected:
        return rejected
    return None


def assert_fixed_rejection(rejected: FrameRejected) -> None:
    kind = type(rejected)
    assert kind in KINDS
    assert rejected.args == (kind.message,)
    assert rejected.__cause__ is None
    assert rejected.__context__ is None
    body = error_frame(rejected, "01JABCDEFGHJKMNPQRSTVWXYZ1", "2026-10-03T18:00:01Z")["body"]
    assert body == {"code": kind.code, "message": kind.message}


@PROPERTY
@given(st.binary(max_size=4096))
def test_any_bytes_decode_or_raise_a_fixed_rejection(raw: bytes) -> None:
    rejected = outcome(raw)
    if rejected is not None:
        assert_fixed_rejection(rejected)


@PROPERTY
@given(st.text(alphabet=st.characters() | st.characters(categories=["Cs"]), max_size=2048))
def test_any_text_including_surrogates_decodes_or_raises_a_fixed_rejection(raw: str) -> None:
    rejected = outcome(raw)
    if rejected is not None:
        assert_fixed_rejection(rejected)


@st.composite
def mutated_fixture(draw: st.DrawFn) -> bytes:
    raw = bytearray(draw(st.sampled_from(FIXTURE_BYTES)))
    for _ in range(draw(st.integers(min_value=1, max_value=4))):
        at = draw(st.integers(min_value=0, max_value=len(raw)))
        action = draw(st.sampled_from(["insert", "delete", "replace"]))
        piece = draw(st.binary(min_size=1, max_size=4) | st.sampled_from([b"[", b"{", b'"', b"\\", b",", b"}", b"]"]))
        if action == "insert":
            raw[at:at] = piece
        elif action == "delete":
            del raw[at : at + len(piece)]
        else:
            raw[at : at + len(piece)] = piece
    return bytes(raw)


@PROPERTY
@given(mutated_fixture())
def test_mutated_fixtures_decode_or_raise_a_fixed_rejection(raw: bytes) -> None:
    rejected = outcome(raw)
    if rejected is not None:
        assert_fixed_rejection(rejected)


@PROPERTY
@given(JSON_VALUES, st.booleans())
def test_any_json_value_in_an_open_object_round_trips_or_is_refused_by_a_rule(value: Any, ascii_only: bool) -> None:
    frame: dict[str, Any] = json.loads((PROTOCOL / "fixtures/valid/skill.result.json").read_text(encoding="utf-8"))
    frame["body"]["output"] = {"value": value}
    raw = json.dumps(frame, ensure_ascii=ascii_only)
    rejected = outcome(raw)
    if rejected is None:
        assert decode(raw, VALIDATOR) == frame
    else:
        assert type(rejected) in (FrameTooLarge, FrameTooDeep), type(rejected)
