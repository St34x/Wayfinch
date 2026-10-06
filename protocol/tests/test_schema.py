# SPDX-License-Identifier: Apache-2.0
"""Meta-validation and contract tests for protocol/envelope.schema.json (envelope v1).

The type list below mirrors the design's envelope table. Golden fixtures live in
protocol/fixtures (T1.03); the inline frames here only prove that every body
sub-schema is wired and that the key constraints hold. This module also holds the shared
constants and frame helpers imported by the other schema test files (topic split, T1.110):
test_schema_bodies.py, test_schema_limits.py, test_schema_edges.py and the test_schema_adversarial*.py files.
"""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

PROTOCOL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROTOCOL_DIR / "envelope.schema.json"
README_PATH = PROTOCOL_DIR / "README.md"
MK_PATH = PROTOCOL_DIR.parent / "mk" / "protocol.mk"

ULID = "01JABCDEFGHJKMNPQRSTVWXYZ0"
ULID2 = "01JXYZ0123456789ABCDEFGHJK"
TS = "2026-10-03T18:00:00Z"

# type -> direction (D→S = device to server, S→D = server to device, both)
DESIGN_TYPES = {
    "hello": "D→S",
    "welcome": "S→D",
    "ping": "both",
    "pong": "both",
    "ack": "D→S",
    "msg.in": "D→S",
    "audio.start": "D→S",
    "audio.chunk": "D→S",
    "audio.end": "D→S",
    "msg.out": "S→D",
    "long.status": "S→D",
    "approval.request": "S→D",
    "approval.response": "D→S",
    "clarify.request": "S→D",
    "clarify.response": "D→S",
    "card.state": "S→D",
    "hud.state": "S→D",
    "skill.manifest": "D→S",
    "skill.call": "S→D",
    "skill.result": "D→S",
    "skill.cancel": "S→D",
    "meeting.start": "D→S",
    "meeting.audio": "D→S",
    "meeting.control": "D→S",
    "meeting.state": "S→D",
    "meeting.hint": "S→D",
    "error": "both",
}

# One minimal valid body per type.
VALID_BODIES = {
    "hello": {
        "appVersion": "0.1.0",
        "protocol": 1,
        "lastAckedSeq": 0,
        "manifestHash": "sha256:" + "a" * 64,
        "glasses": {"model": "G2", "fw": "1.2.3"},
    },
    "welcome": {
        "deviceId": "dev_" + ULID,
        "serverTime": TS,
        "lanes": ["assistant", "work"],
        "limits": {"maxFrame": 65536, "maxAudioSec": 30},
        "resumeFrom": 1,
    },
    "ping": {},
    "pong": {},
    "ack": {"upToSeq": 42},
    "msg.in": {"text": "what time is it", "origin": "ptt", "langHint": "en"},
    "audio.start": {"clipId": ULID, "mime": "audio/ogg;codecs=opus"},
    "audio.chunk": {"clipId": ULID, "chunk": "T2dnUwACAAAAAAAAAAA="},
    "audio.end": {"clipId": ULID, "durationMs": 4500},
    "msg.out": {"kind": "answer", "text": "It is six.", "chatId": "glasses:assistant", "final": True},
    "long.status": {"id": ULID, "status": "sent"},
    "approval.request": {
        "approvalId": "appr-1",
        "summary": "Run: ls -la",
        "choices": ["once", "deny"],
        "expiresAt": TS,
    },
    "approval.response": {"approvalId": "appr-1", "choice": "once", "via": "glasses"},
    "clarify.request": {"clarifyId": "clr-1", "question": "Which one?", "choices": ["A", "B"], "detached": False},
    "clarify.response": {"clarifyId": "clr-1", "answer": "A"},
    "card.state": {"approvalId": "appr-1", "state": "answered"},
    "hud.state": {"hermes": "up", "pendingApprovals": 0, "unreadPushes": 2},
    "skill.manifest": {"manifestVersion": 1, "skills": [{"name": "device.status"}]},
    "skill.call": {
        "callId": ULID,
        "skill": "audio.volume_set",
        "args": {"percent": 30},
        "mode": "act",
        "deadlineMs": 10000,
    },
    "skill.result": {"callId": ULID, "status": "ok", "output": {"percent": 30}},
    "skill.cancel": {"callId": ULID},
    "meeting.start": {
        "meetingId": ULID,
        "langHints": ["ru", "uk", "en", "fr"],
        "hintLevel": "medium",
        "mic": "glasses",
    },
    "meeting.audio": {"meetingId": ULID, "seq": 0, "mime": "audio/ogg;codecs=opus", "chunk": "AAAA"},
    "meeting.control": {"meetingId": ULID, "action": "pause"},
    "meeting.state": {"meetingId": ULID, "state": "active", "provider": "soniox", "elapsedMs": 1000},
    "meeting.hint": {"meetingId": ULID, "hintId": "h1", "kind": "TERM", "text": "ASR = speech recognition"},
    "error": {"code": "lane_forbidden", "message": "lane not allowed"},
}


def frame(type_, body=None, **extra):
    f = {
        "v": 1,
        "id": ULID,
        "type": type_,
        "ts": TS,
        "body": copy.deepcopy(VALID_BODIES[type_] if body is None else body),
    }
    if DESIGN_TYPES[type_] == "S→D":
        f["seq"] = 7
    f.update(extra)
    return f


def errors(validator, instance):
    return [e.message for e in validator.iter_errors(instance)]


def assert_valid(validator, instance):
    errs = errors(validator, instance)
    assert not errs, errs


def assert_invalid(validator, instance):
    assert not validator.is_valid(instance), f"expected invalid: {json.dumps(instance)[:300]}"


# --- schema shape -------------------------------------------------------------------------------


def test_schema_is_draft_2020_12_and_meta_valid(schema):
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_schema_carries_spdx_comment(schema):
    assert "SPDX-License-Identifier: Apache-2.0" in schema["$comment"]


def test_type_enum_matches_design(schema):
    assert set(schema["properties"]["type"]["enum"]) == set(DESIGN_TYPES)


def test_config_change_is_not_a_type(validator):
    # design: config.change goes through skill.call of built-in config.* skills (no new type)
    f = frame("skill.cancel")
    f["type"] = "config.change"
    assert_invalid(validator, f)


@pytest.mark.parametrize("type_", sorted(DESIGN_TYPES))
def test_every_type_has_closed_body_schema(schema, type_):
    body = schema["$defs"]["body." + type_]
    assert body["type"] == "object"
    assert body["additionalProperties"] is False


def _walk_unbounded(node, path, out):
    """Collect strings/arrays/integers/numbers without an upper bound."""
    if isinstance(node, dict):
        t = node.get("type")
        bounded = any(k in node for k in ("enum", "const", "$ref"))
        if t == "string" and not bounded and "maxLength" not in node:
            out.append(path + " (string without maxLength)")
        if t == "array" and "maxItems" not in node:
            out.append(path + " (array without maxItems)")
        if t in ("integer", "number") and not bounded and "maximum" not in node:
            out.append(path + f" ({t} without maximum)")
        if t == "object" and node.get("additionalProperties") is not False and "maxProperties" not in node:
            out.append(path + " (open object without maxProperties)")
        for k, v in node.items():
            _walk_unbounded(v, f"{path}/{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_unbounded(v, f"{path}/{i}", out)


def test_every_value_has_a_limit(schema):
    out = []
    _walk_unbounded(schema, "#", out)
    assert not out, out


# --- positives ----------------------------------------------------------------------------------


@pytest.mark.parametrize("type_", sorted(DESIGN_TYPES))
def test_minimal_frame_per_type_is_valid(validator, type_):
    assert set(VALID_BODIES) == set(DESIGN_TYPES)
    assert_valid(validator, frame(type_))


def test_optional_envelope_fields(validator):
    assert_valid(validator, frame("msg.in", corr=ULID2, lane="assistant"))


# --- envelope negatives -------------------------------------------------------------------------


def test_unknown_type_rejected(validator):
    f = frame("ping")
    f["type"] = "msg.unknown"
    assert_invalid(validator, f)


@pytest.mark.parametrize("v", [0, 2, "1", None])
def test_wrong_version_rejected(validator, v):
    assert_invalid(validator, frame("ping", v=v))


def test_extra_envelope_field_rejected(validator):
    assert_invalid(validator, frame("ping", extra=1))


@pytest.mark.parametrize("field", ["v", "id", "type", "ts", "body"])
def test_required_envelope_fields(validator, field):
    f = frame("ping")
    del f[field]
    assert_invalid(validator, f)


@pytest.mark.parametrize(
    "bad_id", ["", "01jabc", "8" + "0" * 25, "01JABCDEFGHJKMNPQRSTVWXYZI", ULID + "0", ULID[:-1] + "\n"]
)
def test_id_must_be_ulid(validator, bad_id):
    assert_invalid(validator, frame("ping", id=bad_id))
    assert_invalid(validator, frame("ping", corr=bad_id))


@pytest.mark.parametrize("bad_ts", ["2026-10-03", "yesterday", "2026-10-03 18:00:00Z", TS + "\n"])
def test_ts_must_be_rfc3339(validator, bad_ts):
    assert_invalid(validator, frame("ping", ts=bad_ts))


@pytest.mark.parametrize("lane", ["a", "assistant", "work", "lane_2", "x-y", "a" + "b" * 31])
def test_lane_pattern_accepts(validator, lane):
    assert_valid(validator, frame("msg.in", lane=lane))


@pytest.mark.parametrize(
    "lane", ["", "Assistant", "2work", "_x", "-x", "a" + "b" * 32, "wo rk", "wörk", "a.b", "work\n"]
)
def test_lane_pattern_rejects(validator, lane):
    assert_invalid(validator, frame("msg.in", lane=lane))


def test_lane_is_a_pattern_not_an_enum(schema):
    assert schema["properties"]["lane"] == {"$ref": "#/$defs/lane"}
    lane = schema["$defs"]["lane"]
    assert "enum" not in lane
    assert lane["pattern"] == "^[a-z][a-z0-9_-]{0,31}$"


@pytest.mark.parametrize("type_", sorted(t for t, d in DESIGN_TYPES.items() if d == "S→D"))
def test_server_to_device_requires_seq(validator, type_):
    f = frame(type_)
    del f["seq"]
    assert_invalid(validator, f)


@pytest.mark.parametrize("type_", sorted(t for t, d in DESIGN_TYPES.items() if d == "D→S"))
def test_device_to_server_forbids_seq(validator, type_):
    assert_invalid(validator, frame(type_, seq=1))


@pytest.mark.parametrize("seq", [0, -1, 1.5])
def test_seq_positive_integer(validator, seq):
    assert_invalid(validator, frame("msg.out", seq=seq))
