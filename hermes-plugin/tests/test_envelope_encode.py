# SPDX-License-Identifier: Apache-2.0
"""T1.06c: ``encode`` - serialise, validate (schema + rules for the server's direction), measure; a frame that fails
any step raises ``EncodeRefused`` to the producer and nothing is emitted. Refusals never carry frame content."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses.envelope import decode
from glasses.envelope_encode import EncodeRefused, encode
from glasses.envelope_scan import FRAME_CAP

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
SCHEMA = json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
MARKER = "ECHO-MARKER-7f3a"


def load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((PROTOCOL / "fixtures" / "valid" / name).read_text(encoding="utf-8"))
    return loaded


def emit(frame: Any, sink: list[str]) -> None:
    """A producer: what ``encode`` returns goes to the wire (``sink``); a refusal stops it first."""
    sink.append(encode(frame, VALIDATOR))


def refused(frame: Any) -> EncodeRefused:
    sink: list[str] = []
    with pytest.raises(EncodeRefused) as caught:
        emit(frame, sink)
    assert sink == []  # nothing emitted
    assert MARKER not in str(caught.value)
    assert MARKER not in repr(caught.value.args)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None  # no chained serialiser/encoder error holding the frame
    return caught.value


def skill_call_of_size(size: int) -> dict[str, Any]:
    """A schema-valid S→D skill.call whose encoded text is exactly ``size`` UTF-8 bytes (the open ``args`` pads it)."""
    frame = load("skill.call.json")
    frame["body"]["args"] = {"pad": ""}
    base = len(encode(frame, VALIDATOR).encode("utf-8"))
    frame["body"]["args"] = {"pad": "x" * (size - base)}
    return frame


# ---------------------------------------------------------------------------------------- frames that pass


# Which fixtures encode (S→D and both-ways) and which are refused by direction (D→S-only):
# test_envelope_encode_direction.py.


def test_server_frame_text_is_utf8_and_round_trips_through_decode() -> None:
    frame = load("pong.json")
    text = encode(frame, VALIDATOR)
    assert decode(text, VALIDATOR) == frame
    frame = load("msg.out.json")
    frame["body"]["text"] = "Привет, \U0001f600 «ok»"
    assert "Привет" in encode(frame, VALIDATOR)  # UTF-8, not \u escapes: the byte limits measure what is sent


def test_encode_validates_what_it_serialised() -> None:
    seen: list[object] = []

    class Spy:
        def is_valid(self, instance: object) -> bool:
            seen.append(instance)
            return True

    frame = load("msg.out.json")
    frame["body"]["text"] = ("a", "tuple")  # serialises as an array: the schema sees the array, not the tuple
    encode(frame, Spy())
    assert seen == [json.loads(json.dumps(frame))]
    with pytest.raises(EncodeRefused):
        encode(frame, VALIDATOR)


# ---------------------------------------------------------------------------------------- serialise


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), {MARKER}, b"bytes", object()],
    ids=["nan", "inf", "-inf", "set", "bytes", "object"],
)
def test_values_that_are_not_json_are_refused(value: object) -> None:
    frame = load("skill.call.json")
    frame["body"]["args"] = {"x": value}
    assert refused(frame).reason == "not_json"


def test_nesting_too_deep_to_serialise_is_refused() -> None:
    deep: object = MARKER
    for _ in range(100_000):
        deep = [deep]
    frame = load("skill.call.json")
    frame["body"]["args"] = {"x": deep}
    assert refused(frame).reason == "not_json"


def test_keys_that_collide_once_serialised_are_refused() -> None:
    frame = load("skill.call.json")
    frame["body"]["args"] = {1: MARKER, "1": MARKER}
    assert refused(frame).reason == "not_json"


def test_lone_surrogate_is_refused() -> None:
    frame = load("msg.out.json")
    frame["body"]["text"] = MARKER + "\ud800"
    assert refused(frame).reason == "invalid_text"


# ---------------------------------------------------------------------------------------- validate


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.update(extra=MARKER),
        lambda f: f.update(type=MARKER),
        lambda f: f["body"].update(kind=MARKER),
        lambda f: f["body"].pop("text"),
        lambda f: f.update(v=2),
        lambda f: f.update(seq=True),
    ],
    ids=["unknown-field", "unknown-type", "bad-enum", "missing-field", "version", "bool-seq"],
)
def test_schema_invalid_frame_is_refused(mutate: Callable[[dict[str, Any]], object]) -> None:
    frame = load("msg.out.json")
    mutate(frame)
    assert refused(frame).reason == "schema"


def test_injected_schema_decides() -> None:
    class Never:
        def is_valid(self, instance: object) -> bool:
            return False

    with pytest.raises(EncodeRefused, match="schema"):
        encode(load("msg.out.json"), Never())


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda f: f["body"].update(text="я" * 1024 + "a"), "too_many_bytes"),  # 2049 B in 1025 code points
        (lambda f: f.update(ts="2027-02-29T00:00:00Z"), "feb_29"),
        (lambda f: f.update(seq=7.0), "not_integer"),
        (lambda f: f.update(v=1.0), "not_integer"),
    ],
    ids=["byte-limit", "feb-29", "float-seq", "float-v"],
)
def test_rule_breaking_frame_is_refused(mutate: Callable[[dict[str, Any]], object], reason: str) -> None:
    frame = load("msg.out.json")
    mutate(frame)
    assert refused(frame).reason == reason


def test_byte_limit_boundary() -> None:
    frame = load("msg.out.json")
    frame["body"]["text"] = "я" * 1024  # exactly 2048 B
    assert json.loads(encode(frame, VALIDATOR)) == frame


@pytest.mark.parametrize(("name", "with_seq"), [("msg.out.json", False), ("ping.json", True), ("msg.in.json", True)])
def test_seq_against_its_direction_is_refused(name: str, with_seq: bool) -> None:
    # The schema already requires seq on S→D-only types and forbids it on ping/pong and D→S-only types; the
    # direction rules (envelope_rules, from_device=False) check the same again.
    frame = load(name)
    frame.pop("seq", None)
    if with_seq:
        frame["seq"] = 3
    assert refused(frame).reason == "schema"


def test_open_object_too_deep_is_refused() -> None:
    deep: object = 1
    for _ in range(40):
        deep = {"a": deep}
    frame = load("skill.call.json")
    frame["body"]["args"] = {"x": deep}
    assert refused(frame).reason == "too_deep"


# ---------------------------------------------------------------------------------------- measure


def test_frame_cap_boundary() -> None:
    frame = skill_call_of_size(FRAME_CAP)
    assert len(encode(frame, VALIDATOR).encode("utf-8")) == FRAME_CAP
    assert refused(skill_call_of_size(FRAME_CAP + 1)).reason == "frame_too_large"


def test_frame_cap_counts_utf8_bytes() -> None:
    frame = skill_call_of_size(FRAME_CAP)
    frame["body"]["args"]["pad"] = frame["body"]["args"]["pad"][:-2] + "я"  # 2 ASCII B replaced by one 2-B letter
    assert len(encode(frame, VALIDATOR).encode("utf-8")) == FRAME_CAP
    frame["body"]["args"]["pad"] = frame["body"]["args"]["pad"][:-1] + "€"  # 2 B replaced by 3 B
    assert refused(frame).reason == "frame_too_large"


def test_reasons_never_carry_content() -> None:
    frame = load("msg.out.json")
    frame["body"]["text"] = MARKER + "я" * 1020  # 2056 B in 1036 code points: over the byte limit only
    error = refused(frame)
    assert str(error) == "frame not encoded: too_many_bytes"
