# SPDX-License-Identifier: Apache-2.0
"""T1.06b: the envelope rules after the schema - ``seq`` by direction, per-field depth of the open objects, UTF-8
byte limits, Feb 29 of a common year - plus duplicate object keys refused at parse (protocol/README.md, Delivery and
Limits and validation notes). Every rule has a pass and a fail case at its boundary; no rejection echoes input."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses import envelope_rules, envelope_scan
from glasses.envelope import (
    FieldTooLong,
    FrameRejected,
    FrameTooDeep,
    InvalidFrame,
    InvalidJson,
    InvalidSeq,
    decode,
    error_frame,
)
from glasses.envelope_rules import BYTE_LIMITS, D2S_ONLY, S2D_ONLY, Violation, violation
from glasses.generated.protocol_types import FRAME_TYPES

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
SCHEMA = json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
README = (PROTOCOL / "README.md").read_text(encoding="utf-8")
MARKER = "ECHO-MARKER-7f3a"


def load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((PROTOCOL / "fixtures" / name).read_text(encoding="utf-8"))
    return loaded


def nested(depth: int) -> dict[str, object]:
    """An open-object value of ``depth`` levels: an object outside, then arrays and objects alternating."""
    value: object = 1
    for level in range(depth - 1):
        value = {"a": value} if level % 2 else [value]
    return {"a": value}


def utf8_text(size: int) -> str:
    """Exactly ``size`` UTF-8 bytes in at most ``size`` code points (2-byte letters, one ASCII letter if odd)."""
    return "я" * (size // 2) + "a" * (size % 2)


def put(body: dict[str, Any], path: str, value: str) -> None:
    *parents, last = path.split(".")
    for part in parents:
        body = body[part]
    if last.endswith("[]"):
        body[last[:-2]] = [value]
    else:
        body[last] = value


def assert_no_echo(rejected: FrameRejected) -> None:
    frame = error_frame(rejected, "01JABCDEFGHJKMNPQRSTVWXYZ1", "2026-10-03T18:00:01Z")
    assert MARKER not in json.dumps(frame)
    assert MARKER not in str(rejected)
    assert MARKER not in repr(rejected.args)
    assert rejected.__cause__ is None
    assert rejected.__context__ is None
    assert frame["body"] == {"code": rejected.code, "message": rejected.message}
    assert VALIDATOR.is_valid(frame)


# ---------------------------------------------------------------------------------------- seq by direction


def test_every_valid_fixture_with_seq_is_refused_by_direction() -> None:
    with_seq = [path for path in sorted((PROTOCOL / "fixtures" / "valid").glob("*.json")) if "seq" in load_path(path)]
    assert {load_path(path)["type"] for path in with_seq} == S2D_ONLY | {"error"}  # error: the outboxed S→D one
    for path in with_seq:
        with pytest.raises(InvalidSeq):
            decode(path.read_bytes(), VALIDATOR)


def load_path(path: Path) -> dict[str, Any]:
    return load(str(path.relative_to(PROTOCOL / "fixtures")))


def test_d2s_error_with_seq_is_refused_without_echo() -> None:
    frame = load("codec-invalid/d2s-error-with-seq.json")
    frame["body"]["message"] = MARKER
    assert VALIDATOR.is_valid(frame)  # the schema allows seq on error: only the direction refuses it
    with pytest.raises(InvalidSeq) as caught:
        decode(json.dumps(frame), VALIDATOR)
    assert_no_echo(caught.value)
    del frame["seq"]
    assert decode(json.dumps(frame), VALIDATOR)["body"]["message"] == MARKER


def test_s2d_only_type_from_the_device_is_refused_with_or_without_seq() -> None:
    frame = load("valid/msg.out.json")
    with pytest.raises(InvalidSeq):
        decode(json.dumps(frame), VALIDATOR)
    del frame["seq"]
    with pytest.raises(InvalidFrame):  # the schema requires seq on S→D-only types
        decode(json.dumps(frame), VALIDATOR)


@pytest.mark.parametrize(
    ("name", "with_seq", "without_seq"),
    [
        ("valid/msg.out.json", None, Violation.SEQ),
        ("valid/welcome.json", None, Violation.SEQ),
        ("valid/ping.json", Violation.SEQ, None),
        ("valid/pong.json", Violation.SEQ, None),
        ("valid/error.json", None, None),
        ("valid/msg.in.json", Violation.SEQ, None),
    ],
)
def test_seq_rules_when_the_server_sends(name: str, with_seq: Violation | None, without_seq: Violation | None) -> None:
    frame = load(name)
    frame["seq"] = 9
    assert violation(frame, from_device=False) is with_seq
    del frame["seq"]
    assert violation(frame, from_device=False) is without_seq


@pytest.mark.parametrize("frame_type", sorted(FRAME_TYPES))
def test_seq_is_refused_on_every_frame_from_the_device(frame_type: str) -> None:
    frame = {"v": 1, "id": "x", "type": frame_type, "ts": "2026-10-03T18:00:00Z", "body": {}, "seq": 1}
    assert violation(frame, from_device=True) is Violation.SEQ


def test_direction_sets_equal_the_readme_message_types_table() -> None:
    section = README.split("## Message types", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `([a-z.]+)` \| (D→S|S→D|both) \|", section, re.MULTILINE)
    by_dir = {d: {t for t, row_dir in rows if row_dir == d} for d in ("D→S", "S→D", "both")}
    assert by_dir["S→D"] == S2D_ONLY
    assert by_dir["D→S"] == D2S_ONLY
    assert by_dir["both"] == envelope_rules.BOTH_WAYS
    assert set().union(*by_dir.values()) == set(FRAME_TYPES)


# ---------------------------------------------------------------------------------------- per-field depth


@pytest.mark.parametrize(("depth", "accepted"), [(32, True), (33, False)])
def test_skill_result_output_depth_boundary(depth: int, accepted: bool) -> None:
    frame = load("valid/skill.result.json")
    frame["body"]["output"] = nested(depth)
    raw = json.dumps(frame)
    assert raw.count("[") + raw.count("{") == depth + 2  # 35 brackets for depth 33: the pre-scan lets it through
    if accepted:
        assert decode(raw, VALIDATOR)["body"] == frame["body"]
    else:
        with pytest.raises(FrameTooDeep):
            decode(raw, VALIDATOR)


@pytest.mark.parametrize(("depth", "expected"), [(32, None), (33, Violation.TOO_DEEP)])
def test_skill_manifest_item_depth_boundary(depth: int, expected: Violation | None) -> None:
    frame = load("valid/skill.manifest.json")
    frame["body"]["skills"] = [{"ok": 1}, nested(depth)]
    assert VALIDATOR.is_valid(frame)
    assert violation(frame, from_device=True) is expected  # depth 33 = 36 brackets: decode refuses it unparsed


@pytest.mark.parametrize(("depth", "expected"), [(32, None), (33, Violation.TOO_DEEP)])
def test_skill_call_args_depth_boundary(depth: int, expected: Violation | None) -> None:
    frame = load("valid/skill.call.json")
    frame["body"]["args"] = nested(depth)
    assert VALIDATOR.is_valid(frame)
    assert violation(frame, from_device=False) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("codec-valid/s2d-skill-call-args-depth-32.json", None),
        ("codec-invalid/s2d-skill-call-args-depth-33.json", Violation.TOO_DEEP),
    ],
)
def test_skill_call_args_depth_fixtures(name: str, expected: Violation | None) -> None:
    assert violation(load(name), from_device=False) is expected


def test_depth_counts_arrays_and_ignores_scalars() -> None:
    frame = load("valid/skill.result.json")
    frame["body"]["output"] = {"wide": list(range(500)), "text": "[" * 100, "deep": nested(31)}
    assert violation(frame, from_device=True) is None
    frame["body"]["output"] = {"deep": [nested(31)]}  # output 1 + array 1 + 31 = 33
    assert violation(frame, from_device=True) is Violation.TOO_DEEP


def test_depth_rejection_does_not_echo() -> None:
    frame = load("valid/skill.result.json")
    frame["body"]["output"] = {MARKER: nested(32)}
    with pytest.raises(FrameTooDeep) as caught:
        decode(json.dumps(frame), VALIDATOR)
    assert_no_echo(caught.value)


# ---------------------------------------------------------------------------------------- UTF-8 byte limits


def readme_byte_limits() -> dict[str, int]:
    section = README.split("| field | max UTF-8 bytes |", 1)[1].split("\n\n", 1)[0]
    limits: dict[str, int] = {}
    for fields, limit in re.findall(r"^\| (.+?) \| ([0-9]+) \|$", section, re.MULTILINE):
        names = ["msg.out.text+longRef"] if "with `longRef`" in fields else re.findall(r"`([^`]+)`", fields)
        limits.update(dict.fromkeys(names, int(limit)))
    return limits


def test_byte_limits_equal_the_readme_limits_table() -> None:
    ours = {f"{frame_type}.{path}": limit for frame_type, path, limit in BYTE_LIMITS}
    ours["msg.out.text+longRef"] = envelope_rules.MSG_OUT_TEXT_WITH_LONGREF
    assert ours == readme_byte_limits()


def limited_frame(frame_type: str, path: str, size: int) -> dict[str, Any]:
    frame = load("valid/msg.out-long-ref.json" if path.startswith("longRef") else f"valid/{frame_type}.json")
    put(frame["body"], path, "A" * size if path == "chunk" else utf8_text(size))  # base64: ASCII only
    return frame


@pytest.mark.parametrize(("frame_type", "path", "limit"), BYTE_LIMITS, ids=[f"{t}.{p}" for t, p, _ in BYTE_LIMITS])
def test_byte_limit_boundary(frame_type: str, path: str, limit: int) -> None:
    from_device = frame_type not in S2D_ONLY
    at_limit = limited_frame(frame_type, path, limit)
    assert VALIDATOR.is_valid(at_limit)
    assert violation(at_limit, from_device=from_device) is None
    assert violation(limited_frame(frame_type, path, limit + 1), from_device=from_device) is Violation.TOO_MANY_BYTES


DEVICE_TEXT_LIMITS = [row for row in BYTE_LIMITS if row[0] not in S2D_ONLY and row[1] != "chunk"]


@pytest.mark.parametrize(
    ("frame_type", "path", "limit"), DEVICE_TEXT_LIMITS, ids=[f"{t}.{p}" for t, p, _ in DEVICE_TEXT_LIMITS]
)
def test_device_frame_over_its_byte_limit_is_refused_by_decode(frame_type: str, path: str, limit: int) -> None:
    assert decode(json.dumps(limited_frame(frame_type, path, limit)), VALIDATOR)["type"] == frame_type
    over = limited_frame(frame_type, path, limit + 1)
    assert VALIDATOR.is_valid(over)  # within maxLength (code points): only the byte rule refuses it
    with pytest.raises(FieldTooLong):
        decode(json.dumps(over), VALIDATOR)


@pytest.mark.parametrize(("size", "expected"), [(512, None), (513, Violation.TOO_MANY_BYTES)])
def test_msg_out_text_with_long_ref_boundary(size: int, expected: Violation | None) -> None:
    frame = load("valid/msg.out-long-ref.json")
    frame["body"]["text"] = utf8_text(size)
    assert VALIDATOR.is_valid(frame)
    assert violation(frame, from_device=False) is expected
    del frame["body"]["longRef"]
    assert violation(frame, from_device=False) is None  # 2048 B without longRef


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("codec-valid/s2d-msg-out-text-2048b-emoji.json", None),
        ("codec-invalid/s2d-msg-out-text-2052b-emoji.json", Violation.TOO_MANY_BYTES),
        ("codec-valid/s2d-msg-out-text-512b-long-ref-emoji.json", None),
        ("codec-invalid/s2d-msg-out-text-516b-long-ref-emoji.json", Violation.TOO_MANY_BYTES),
        ("codec-valid/s2d-msg-out-full-text-4096b-cyrillic.json", None),
        ("codec-invalid/s2d-msg-out-full-text-4098b-cyrillic.json", Violation.TOO_MANY_BYTES),
    ],
)
def test_msg_out_byte_fixtures(name: str, expected: Violation | None) -> None:
    assert violation(load(name), from_device=False) is expected


def test_msg_in_byte_fixtures_through_decode() -> None:
    assert decode((PROTOCOL / "fixtures/codec-valid/d2s-msg-in-text-2000b-cyrillic.json").read_bytes(), VALIDATOR)
    with pytest.raises(FieldTooLong):
        decode((PROTOCOL / "fixtures/codec-invalid/d2s-msg-in-text-2002b-cyrillic.json").read_bytes(), VALIDATOR)


def test_byte_limit_rejection_does_not_echo() -> None:
    frame = load("valid/msg.in.json")
    frame["body"]["text"] = MARKER + utf8_text(2000)
    with pytest.raises(FieldTooLong) as caught:
        decode(json.dumps(frame), VALIDATOR)
    assert_no_echo(caught.value)


# ---------------------------------------------------------------------------------------- Feb 29


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2028-02-29T00:00:00Z", None),
        ("2027-02-29T00:00:00Z", Violation.FEB_29),
        ("2000-02-29T23:59:59.999999999+14:00", None),
        ("1900-02-29T00:00:00Z", Violation.FEB_29),
        ("2100-02-29T00:00:00-01:00", Violation.FEB_29),
        ("0000-02-29T00:00:00Z", None),
        ("2027-02-28T00:00:00Z", None),
        ("2027-03-29T00:00:00Z", None),
    ],
)
def test_feb_29_in_every_timestamp_field(stamp: str, expected: Violation | None) -> None:
    for name, field, from_device in [
        ("valid/msg.in.json", None, True),
        ("valid/welcome.json", "serverTime", False),
        ("valid/approval.request.json", "expiresAt", False),
    ]:
        frame = load(name)
        if field is None:
            frame["ts"] = stamp
        else:
            frame["body"][field] = stamp
        assert VALIDATOR.is_valid(frame), stamp  # the schema's pattern lets Feb 29 through in every year
        assert violation(frame, from_device=from_device) is expected, (name, stamp)


def test_timestamp_fields_equal_the_schema() -> None:
    found = set()
    for name, body in SCHEMA["$defs"].items():
        for field, spec in body.get("properties", {}).items():
            if name.startswith("body.") and spec.get("$ref") == "#/$defs/timestamp":
                found.add((name[len("body.") :], field))
    assert found == set(envelope_rules.TIMESTAMP_FIELDS)
    assert SCHEMA["properties"]["ts"] == {"$ref": "#/$defs/timestamp"}


def test_feb_29_fixtures_through_decode_without_echo() -> None:
    assert decode((PROTOCOL / "fixtures/codec-valid/d2s-ts-2028-02-29.json").read_bytes(), VALIDATOR)
    frame = load("codec-invalid/d2s-ts-2027-02-29.json")
    frame["body"]["text"] = MARKER
    with pytest.raises(InvalidFrame) as caught:
        decode(json.dumps(frame), VALIDATOR)
    assert_no_echo(caught.value)


# ---------------------------------------------------------------------------------------- duplicate keys


def duplicated(where: str) -> str:
    frame = load("valid/skill.result.json")
    raw = json.dumps(frame)
    if where == "top":
        return raw.replace('"v": 1', f'"v": 1, "v": 1, "{MARKER}": 1', 1)
    if where == "body":
        return raw.replace('"status": "ok"', f'"status": "ok", "status": "{MARKER}"', 1)
    if where == "nested":
        return raw.replace('"percent": 30', f'"percent": 30, "x": [{{"k": 1, "k": "{MARKER}"}}]', 1)
    return raw.replace('"type": "skill.result"', f'"type": "skill.result", "\\u0074ype": "{MARKER}"', 1)


@pytest.mark.parametrize("where", ["top", "body", "nested", "escaped"])
def test_duplicate_keys_are_refused_without_echo(where: str) -> None:
    with pytest.raises(InvalidJson) as caught:
        decode(duplicated(where), VALIDATOR)
    assert_no_echo(caught.value)


def test_the_same_key_in_sibling_objects_is_accepted() -> None:
    frame = load("valid/skill.result.json")
    frame["body"]["output"] = {"a": {"k": 1}, "b": {"k": 2}, "c": [{"k": 3}, {"k": 4}]}
    assert decode(json.dumps(frame), VALIDATOR) == frame


def test_two_top_level_type_keys_are_refused_as_duplicates() -> None:
    raw = json.dumps(load("valid/msg.in.json")).replace('"type": "msg.in"', '"type": "audio.chunk", "type": "msg.in"')
    assert envelope_scan.scan(raw).top_level_types == frozenset({"audio.chunk", "msg.in"})
    assert json.loads(raw)["type"] == "msg.in"  # a parser that keeps the last key would read msg.in
    with pytest.raises(InvalidJson) as caught:
        decode(raw, VALIDATOR)
    assert_no_echo(caught.value)


def test_rule_codes_are_distinct_and_schema_valid() -> None:
    kinds = [InvalidSeq, FieldTooLong]
    assert len({kind.code for kind in kinds} | {FrameTooDeep.code, InvalidFrame.code, InvalidJson.code}) == 5
    for kind in kinds:
        assert VALIDATOR.is_valid(error_frame(kind(), "01JABCDEFGHJKMNPQRSTVWXYZ1", "2026-10-03T18:00:01Z"))


def test_violation_does_not_mutate_the_frame() -> None:
    frame = load("valid/msg.out-long-ref.json")
    before = copy.deepcopy(frame)
    violation(frame, from_device=False)
    assert frame == before
