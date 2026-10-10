# SPDX-License-Identifier: Apache-2.0
"""T1.06b: integer fields hold JSON integer literals. JSON Schema counts ``1.0``/``1e0`` as integers; the phone's codec
reads these fields as ``Int``, so ``envelope_rules`` refuses any float outside the open objects. The contract tests
derive every integer field from the schema and prove the rule covers each one; no rejection echoes input."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses import envelope_rules
from glasses.envelope import FrameRejected, InvalidFrame, decode, error_frame
from glasses.envelope_rules import Violation, violation

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
SCHEMA = json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
MARKER = "ECHO-MARKER-7f3a"


def load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((PROTOCOL / "fixtures" / name).read_text(encoding="utf-8"))
    return loaded


def assert_no_echo(rejected: FrameRejected) -> None:
    frame = error_frame(rejected, "01JABCDEFGHJKMNPQRSTVWXYZ1", "2026-10-03T18:00:01Z")
    assert MARKER not in json.dumps(frame)
    assert MARKER not in str(rejected)
    assert rejected.__cause__ is None
    assert rejected.__context__ is None
    assert VALIDATOR.is_valid(frame)


def schema_integer_fields(spec: dict[str, Any], path: str) -> Iterator[str]:
    """Every path the schema types as ``integer`` (or pins to an integer ``const``, like ``v``) under ``spec``."""
    if "$ref" in spec:
        yield from schema_integer_fields(SCHEMA["$defs"][spec["$ref"].removeprefix("#/$defs/")], path)
    const = spec.get("const")
    if spec.get("type") == "integer" or (isinstance(const, int) and not isinstance(const, bool)):
        yield path
    for key, child in spec.get("properties", {}).items():
        yield from schema_integer_fields(child, f"{path}.{key}" if path else key)
    if isinstance(spec.get("items"), dict):
        yield from schema_integer_fields(spec["items"], path + "[]")


def schema_types(spec: object) -> Iterator[str]:
    if isinstance(spec, dict):
        kind = spec.get("type")
        if isinstance(kind, str):
            yield kind
        elif isinstance(kind, list):
            yield from kind
        for child in spec.values():
            yield from schema_types(child)
    elif isinstance(spec, list):
        for child in spec:
            yield from schema_types(child)


def integer_slots(value: object, path: str, frame_type: str) -> Iterator[tuple[str, list[Any] | dict[str, Any], Any]]:
    """(path, container, key) of every int outside the open objects of a decoded frame."""
    open_paths = {f"body.{p.removesuffix('[]')}" for owner, p in envelope_rules.OPEN_FIELDS if owner == frame_type}
    if path in open_paths:
        return
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else []
    for key, child in items:
        child_path = f"{path}.{key}" if isinstance(key, str) else path + "[]"
        child_path = child_path.removeprefix(".")
        if isinstance(child, int) and not isinstance(child, bool):
            yield child_path, value, key
        else:
            yield from integer_slots(child, child_path, frame_type)


def test_schema_types_no_field_as_number() -> None:
    # The rule "no float outside the open objects" equals "integer fields hold integer literals" only while this holds.
    assert "number" not in set(schema_types(SCHEMA))


def test_every_integer_field_of_the_schema_refuses_a_float_spelling() -> None:
    expected = set(schema_integer_fields(SCHEMA, ""))  # the envelope's own fields
    for name, spec in SCHEMA["$defs"].items():
        if name.startswith("body."):
            expected |= {f"{name.removeprefix('body.')}:body.{p}" for p in schema_integer_fields(spec, "")}
    mutated = set()
    hint_with_ttl = load("valid/meeting.hint.json")
    hint_with_ttl["body"]["ttlMs"] = 10_000  # the one optional integer field no valid fixture sets
    frames = [load(f"valid/{path.name}") for path in sorted((PROTOCOL / "fixtures" / "valid").glob("*.json"))]
    for frame in [*frames, hint_with_ttl]:
        from_device = "seq" not in frame
        assert violation(frame, from_device=from_device) is None, frame["type"]
        for slot, container, key in list(integer_slots(frame, "", frame["type"])):
            container[key] = float(container[key])
            assert VALIDATOR.is_valid(frame), slot  # the schema alone lets 1.0 through
            assert violation(frame, from_device=from_device) is Violation.NOT_INTEGER, slot
            container[key] = int(container[key])
            mutated.add(f"{frame['type']}:{slot}" if slot.startswith("body.") else slot)
    assert {"v", "seq"} <= expected
    assert expected <= mutated, sorted(expected - mutated)


@pytest.mark.parametrize("spelling", ["1", "1.0", "1e0", "1E0", "10e-1"])
def test_integer_spelling_through_decode(spelling: str) -> None:
    raw = json.dumps(load("valid/msg.in.json")).replace('"v": 1', '"v": ' + spelling, 1)
    raw = raw.replace('"text": "', f'"text": "{MARKER}', 1)
    if spelling == "1":
        assert decode(raw, VALIDATOR)["v"] == 1
        return
    with pytest.raises(InvalidFrame) as caught:
        decode(raw, VALIDATOR)
    assert_no_echo(caught.value)


def test_floats_inside_open_objects_are_data() -> None:
    frame = load("valid/skill.result.json")
    frame["body"]["output"] = {"ratio": 1.0, "list": [2.5, {"x": 1e0}]}
    assert decode(json.dumps(frame), VALIDATOR) == frame
    call = load("valid/skill.call.json")
    call["body"]["args"] = {"x": 1.0}
    assert violation(call, from_device=False) is None
    call["body"]["deadlineMs"] = float(call["body"]["deadlineMs"])
    assert violation(call, from_device=False) is Violation.NOT_INTEGER
