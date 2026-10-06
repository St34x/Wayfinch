# SPDX-License-Identifier: Apache-2.0
"""Adversarial tests for protocol/envelope.schema.json (T1.02): card.state combos, open fields, nulls,
timestamps, enum near-misses. Conventions and helpers: test_schema_adversarial.py.
"""

import json

import pytest

from test_schema import TS, ULID
from test_schema import VALID_BODIES as B
from test_schema_adversarial import bad, body, fr, ok

# --- card.state invalid combos ------------------------------------------------------------------


@pytest.mark.parametrize(
    "b",
    [
        {"approvalId": "a", "state": "detached"},
        {"approvalId": "a", "state": "dismissed"},
        {"clarifyId": "c", "state": "expired"},
        {"approvalId": "a", "clarifyId": "c", "state": "answered"},
        {"approvalId": "a", "clarifyId": "c", "state": "expired"},
        {"approvalId": "a", "clarifyId": "c", "state": "detached"},
        {"state": "answered"},
        {"state": "expired"},
        {"approvalId": "a"},
        {"clarifyId": "c"},
        {"approvalId": "a", "state": "pending"},
        {"clarifyId": "c", "state": "pending"},
        {"clarifyId": "c", "state": "Answered"},
        {"clarifyId": "c", "state": None},
        {"approvalId": None, "state": "answered"},
        {"clarifyId": "c", "state": "answered", "approvalId": None},
        {"approvalId": "", "state": "answered"},
        {"clarifyId": "c", "state": "detached", "reason": "moved on"},
        {"cardId": "c", "state": "answered"},
    ],
)
def test_card_state_invalid_combos(v, b):
    bad(v, fr("card.state", body=b))


def test_card_state_requires_seq(v):
    f = fr("card.state", body={"clarifyId": "c", "state": "detached"})
    del f["seq"]
    bad(v, f)


# --- deeply nested / large open fields ----------------------------------------------------------


def _nested_json(depth):
    return "[" * depth + "1" + "]" * depth


def test_contract_open_fields_depth_not_bounded_by_schema(v):
    # skill.call.args / skill.result.output only carry maxProperties at their top level. Depth and inner sizes
    # are bounded only by the codec's 64 KiB frame limit, so the codec must check size BEFORE json.loads and
    # cap nesting depth (Python's json recursion limit is ~ interpreter recursion limit).
    raw = (
        '{"v":1,"id":"' + ULID + '","type":"skill.call","ts":"' + TS + '","seq":1,"body":{"callId":"c","skill":"a.b",'
        '"mode":"read","deadlineMs":1,"args":{"a":' + _nested_json(500) + "}}}"
    )
    ok(v, json.loads(raw))
    inner = {str(i): i for i in range(1000)}
    ok(v, fr("skill.call", body=body("skill.call", args={"a": inner})))
    ok(v, fr("skill.result", body=body("skill.result", output={"a": {"b": "x" * 100000}})))


def test_open_fields_top_level_property_caps(v):
    ok(v, fr("skill.call", body=body("skill.call", args={str(i): i for i in range(64)})))
    bad(v, fr("skill.call", body=body("skill.call", args={str(i): i for i in range(65)})))
    ok(v, fr("skill.result", body=body("skill.result", output={str(i): i for i in range(256)})))
    bad(v, fr("skill.result", body=body("skill.result", output={str(i): i for i in range(257)})))
    bad(v, fr("skill.manifest", body=body("skill.manifest", skills=[{str(i): i for i in range(65)}])))
    bad(v, fr("skill.manifest", body=body("skill.manifest", skills=[{}] * 257)))


@pytest.mark.parametrize("args", [None, [], "x", 1, True])
def test_open_fields_must_be_objects(v, args):
    bad(v, fr("skill.call", body=body("skill.call", args=args)))
    bad(v, fr("skill.result", body=body("skill.result", output=args)))


def test_skill_manifest_items_must_be_objects(v):
    for item in (None, "device.status", 1, []):
        bad(v, fr("skill.manifest", body=body("skill.manifest", skills=[item])))


# --- null values ---------------------------------------------------------------------------------

# Every optional field set to JSON null must be rejected (absent ≠ null).
OPTIONAL_NULLS = [
    ("msg.in", "langHint"),
    ("msg.out", "notice"),
    ("msg.out", "longRef"),
    ("approval.request", "detail"),
    ("skill.result", "output"),
    ("skill.result", "error"),
    ("meeting.start", "prepTerms"),
    ("meeting.start", "langHints"),
    ("meeting.control", "hintLevel"),
    ("meeting.state", "error"),
    ("meeting.hint", "ttlMs"),
    ("hud.state", "longChannel"),
]


@pytest.mark.parametrize("t,field", OPTIONAL_NULLS)
def test_optional_body_field_null_rejected(v, t, field):
    bad(v, fr(t, body=body(t, **{field: None})))


@pytest.mark.parametrize("t", sorted(t for t, b in B.items() if b))
def test_required_body_field_null_rejected(v, t):
    for field in B[t]:
        bad(v, fr(t, body=body(t, **{field: None})))


@pytest.mark.parametrize("field", ["v", "id", "type", "ts", "body", "corr", "lane", "seq"])
def test_envelope_field_null_rejected(v, field):
    f = fr("msg.out")
    f[field] = None
    bad(v, f)


def test_nested_nulls_rejected(v):
    bad(v, fr("hello", body=body("hello", glasses={"model": None, "fw": "1"})))
    bad(v, fr("welcome", body=body("welcome", limits={"maxFrame": None, "maxAudioSec": 30})))
    bad(v, fr("welcome", body=body("welcome", lanes=[None])))
    bad(v, fr("clarify.request", body=body("clarify.request", choices=[None])))
    bad(v, fr("approval.request", body=body("approval.request", choices=[None])))
    bad(v, fr("msg.out", body=body("msg.out", kind="notice", notice={"kind": "system", "ref": None})))


# --- timestamps ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ts",
    [
        "2026-10-03T18:00:00Z",
        "2026-10-03T18:00:00.123456789Z",
        "2026-10-03T18:00:00+02:00",
        "2026-10-03T18:00:00-11:30",
    ],
)
def test_timestamp_accepted(v, ts):
    ok(v, fr("ping", ts=ts))


@pytest.mark.parametrize(
    "ts",
    [
        "2026-10-03T18:00:00",  # no offset
        "2026-10-03T18:00:00.1234567890Z",  # 10 fraction digits
        "2026-10-03t18:00:00Z",
        "2026-10-03T18:00Z",
        "1759514400",
        "2026-10-03T18:00:00+0200",
        "２０２６-10-03T18:00:00Z",
        "",
    ],
)
def test_timestamp_shape_rejected(v, ts):
    bad(v, fr("ping", ts=ts))


@pytest.mark.parametrize(
    "ts",
    [
        "2026-13-01T00:00:00Z",
        "2026-00-10T00:00:00Z",
        "2026-10-32T00:00:00Z",
        "2026-10-03T24:00:00Z",
        "2026-10-03T18:60:00Z",
        "2026-10-03T18:00:61Z",
        "2026-10-03T18:00:00+25:00",
    ],
)
def test_timestamp_out_of_range_rejected(v, ts):
    bad(v, fr("ping", ts=ts))


# --- misc enums / design consistency ------------------------------------------------------------


@pytest.mark.parametrize(
    "t,field,value",
    [
        ("msg.in", "origin", "PTT"),
        ("msg.in", "origin", "voice"),
        ("msg.out", "kind", "Answer"),
        ("msg.out", "kind", "error"),
        ("long.status", "status", "pending"),
        ("approval.response", "choice", "always"),
        ("approval.response", "choice", "session"),
        ("approval.response", "via", "watch"),
        ("hud.state", "hermes", "down"),
        ("skill.call", "mode", "write"),
        ("skill.result", "status", "timeout"),
        ("meeting.start", "mic", "phone"),
        ("meeting.start", "hintLevel", "max"),
        ("meeting.control", "action", "start"),
        ("meeting.state", "state", "stopped"),
        ("meeting.hint", "kind", "term"),
        ("audio.start", "mime", "audio/ogg; codecs=opus"),
        ("audio.start", "mime", "AUDIO/OGG;CODECS=OPUS"),
    ],
)
def test_enum_near_misses_rejected(v, t, field, value):
    bad(v, fr(t, body=body(t, **{field: value})))


@pytest.mark.parametrize("choices", [["once", "deny", "once"], ["always"], ["session"], ["once", "Deny"], "once"])
def test_approval_choices_subset_only(v, choices):
    bad(v, fr("approval.request", body=body("approval.request", choices=choices)))


def test_msg_out_long_ref_channel_and_profile(v):
    lr = {"id": ULID, "channel": "telegram", "profile": "work", "status": "sent"}
    # channel is a platform-name pattern (any configured glasses.long_answers.platform), not const "telegram"
    ok(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, channel="email"))))
    bad(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, channel="Email"))))
    bad(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, channel="tele gram"))))
    bad(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, profile="Work"))))
    bad(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, profile="work\n"))))
    bad(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, profile="p" * 65))))
    bad(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, extra=1))))


def test_hello_nested_glasses_closed(v):
    bad(v, fr("hello", body=body("hello", glasses={"model": "G2", "fw": "1", "serial": "S1"})))
    bad(v, fr("hello", body=body("hello", glasses={"model": "G2"})))
    bad(v, fr("hello", body=body("hello", manifestHash="sha256:" + "a" * 64 + "\n")))
