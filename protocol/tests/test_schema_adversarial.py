# SPDX-License-Identifier: Apache-2.0
"""Adversarial tests for protocol/envelope.schema.json.

On top of test_schema.py. Each block names the attack. Tests marked
`xfail(strict=True)` record a known gap: they flip to XPASS (= suite failure) once
the schema is fixed, so the marker must then be removed.
Tests named `test_contract_*` pin what the schema deliberately leaves to the codec
(T1.06): the schema accepts it, so the codec has to enforce it.
This file keeps the shared helpers; test_schema_adversarial_scalars.py and
test_schema_adversarial_objects.py hold the rest.
"""

import json

import pytest

import test_schema as base

ULID = base.ULID
TS = base.TS
B = base.VALID_BODIES


def fr(type_, body=None, **extra):
    return base.frame(type_, body, **extra)


def body(type_, **changes):
    b = json.loads(json.dumps(B[type_]))
    b.update(changes)
    return b


def ok(v, inst):
    errs = [e.message for e in v.iter_errors(inst)]
    assert not errs, errs


def bad(v, inst):
    assert not v.is_valid(inst), f"expected invalid: {json.dumps(inst, ensure_ascii=True)[:300]}"


# --- unknown type / type-body mismatch ----------------------------------------------------------


@pytest.mark.parametrize(
    "t", ["Hello", "hello ", " hello", "HELLO", "msg_in", "msg", "msg.", ".in", "config.change", "", "pong\n"]
)
def test_unknown_or_near_miss_type_rejected(v, t):
    f = fr("ping")
    f["type"] = t
    bad(v, f)


@pytest.mark.parametrize("t", [None, 1, True, ["ping"], {"ping": 1}])
def test_type_wrong_json_type_rejected(v, t):
    f = fr("ping")
    f["type"] = t
    bad(v, f)


# Every type with a non-empty body: the body of every OTHER non-empty type must be rejected.
_NONEMPTY = sorted(t for t, b in B.items() if b)


@pytest.mark.parametrize("t,other", [(a, b) for a in _NONEMPTY for b in _NONEMPTY if a != b])
def test_cross_type_body_mismatch_rejected(v, t, other):
    bad(v, fr(t, body=B[other]))


@pytest.mark.parametrize("t", _NONEMPTY)
def test_empty_body_rejected_where_fields_required(v, t):
    bad(v, fr(t, body={}))


@pytest.mark.parametrize("t", ["ping", "pong"])
def test_ping_pong_body_must_be_empty(v, t):
    bad(v, fr(t, body={"text": "x"}))
    bad(v, fr(t, body=B["ack"]))


@pytest.mark.parametrize("b", [None, [], "x", 0, True])
def test_body_must_be_object(v, b):
    f = fr("ping")
    f["body"] = b
    bad(v, f)


# --- extra top-level keys -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["Type", "ID", "V", "__proto__", "constructor", "$schema", "", " ", "type ", "bodyx", "sig"]
)
def test_extra_top_level_key_rejected(v, key):
    f = fr("ping")
    f[key] = 1
    bad(v, f)


def test_top_level_must_be_object(v):
    for inst in ([], "x", 1, None, [fr("ping")]):
        bad(v, inst)


# --- string limits at limit / limit+1 -----------------------------------------------------------

# (type, field path, limit). The schema's maxLength is a code-point bound equal to the UTF-8 byte limit that the
# codec enforces (README "Limits"); these tests exercise the schema, i.e. code points.
LIMITS = [
    ("msg.in", "text", 2000),
    ("msg.out", "text", 2048),
    ("approval.request", "summary", 300),
    ("approval.request", "detail", 4000),
    ("clarify.request", "question", 2000),
    ("clarify.response", "answer", 2000),
    ("meeting.hint", "text", 180),
    ("error", "message", 500),
    ("skill.result", "error", 500),
    ("hello", "appVersion", 64),
    ("meeting.state", "provider", 64),
]


@pytest.mark.parametrize("t,field,limit", LIMITS)
@pytest.mark.parametrize("ch", ["x", "ж", "\u05e9", "\U0001f600"], ids=["ascii", "cyrillic", "hebrew", "emoji"])
def test_string_limit_exact_and_plus_one(v, t, field, limit, ch):
    ok(v, fr(t, body=body(t, **{field: ch * limit})))
    bad(v, fr(t, body=body(t, **{field: ch * (limit + 1)})))


def test_long_ref_full_text_limit(v):
    lr = {"id": ULID, "channel": "telegram", "profile": "work", "status": "sent"}
    ok(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, fullText="x" * 4096))))
    bad(v, fr("msg.out", body=body("msg.out", longRef=dict(lr, fullText="x" * 4097))))


def test_contract_full_text_byte_limit_is_the_codecs(v):
    # README: `fullText` ≤ 4096 UTF-8 bytes, enforced by the codec. The schema can only count code points, so
    # 4096 Cyrillic chars (8 KiB UTF-8) pass the schema and the codec must reject them.
    lr = {"id": ULID, "channel": "telegram", "profile": "work", "status": "sent", "fullText": "ж" * 4096}
    assert len(lr["fullText"].encode("utf-8")) == 8192
    ok(v, fr("msg.out", body=body("msg.out", longRef=lr)))


def test_clarify_choice_item_limit(v):
    ok(v, fr("clarify.request", body=body("clarify.request", choices=["я" * 200] * 4)))
    bad(v, fr("clarify.request", body=body("clarify.request", choices=["я" * 201])))
    bad(v, fr("clarify.request", body=body("clarify.request", choices=[""])))


def test_meeting_prep_term_item_limit(v):
    ok(v, fr("meeting.start", body=body("meeting.start", prepTerms=["t" * 100])))
    bad(v, fr("meeting.start", body=body("meeting.start", prepTerms=["t" * 101])))


def test_opaque_id_limit(v):
    ok(v, fr("skill.cancel", body={"callId": "a" * 128}))
    bad(v, fr("skill.cancel", body={"callId": "a" * 129}))
    bad(v, fr("skill.cancel", body={"callId": ""}))
    for bad_id in ("a b", "a/b", "a\nb", "\u0430", "a\u200bb", "id\n"):
        bad(v, fr("skill.cancel", body={"callId": bad_id}))


def test_error_code_limit(v):
    ok(v, fr("error", body={"code": "a" * 64, "message": ""}))
    bad(v, fr("error", body={"code": "a" * 65, "message": ""}))
    for code in ("Lane_forbidden", "lane-forbidden", "1abc", "lane forbidden", "lane_forbidden\n"):
        bad(v, fr("error", body={"code": code, "message": ""}))


# --- unicode edge cases in text fields ----------------------------------------------------------

UNICODE_TEXTS = {
    "rtl_hebrew": "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd",
    "rtl_arabic": "\u0645\u0631\u062d\u0628\u0627",
    "bidi_override": "Run: \u202erm -rf ~\u202c",
    "zero_width": "a\u200bb\u200cc\u200dd\u2060e\ufeff",
    "emoji_zwj": "\U0001f469\u200d\U0001f4bb \U0001f1fa\U0001f1e6",
    "cyrillic": "Привет, мир — ёЁ ґ є ї",
    "combining": "e\u0301\u0301\u0301",
    "line_separators": "a\u2028b\u2029c",
    "newlines_tabs": "line1\nline2\r\n\tend",
}

TEXT_FIELDS = [
    ("msg.in", "text"),
    ("msg.out", "text"),
    ("approval.request", "summary"),
    ("clarify.request", "question"),
    ("clarify.response", "answer"),
    ("meeting.hint", "text"),
]


# Approval/clarify display fields reject bidi controls (display spoofing on the glasses).
BIDI_GUARDED = {("approval.request", "summary"), ("clarify.request", "question")}


@pytest.mark.parametrize("t,field", TEXT_FIELDS)
@pytest.mark.parametrize("name", sorted(UNICODE_TEXTS))
def test_unicode_free_text_accepted(v, t, field, name):
    # Free-text fields are content: the schema must not reject legitimate scripts or formatting characters.
    inst = fr(t, body=body(t, **{field: UNICODE_TEXTS[name][:60]}))
    if name == "bidi_override" and (t, field) in BIDI_GUARDED:
        bad(v, inst)
    else:
        ok(v, inst)


def test_limits_count_code_points_not_utf16_or_graphemes(v):
    # Python jsonschema counts code points. An emoji outside the BMP is 1 code point but 2 UTF-16 units
    # (Kotlin String.length); a ZWJ family is 5 code points but 1 grapheme. Both stacks must count the same way.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    ok(v, fr("msg.in", body={"text": family * 400, "origin": "ptt"}))  # 2000 code points, 400 graphemes
    bad(v, fr("msg.in", body={"text": family * 401, "origin": "ptt"}))
    ok(v, fr("meeting.hint", body=body("meeting.hint", text="\U0001f600" * 60)))  # 120 UTF-16 units


@pytest.mark.parametrize(
    "lane",
    [
        "\u0430ssistant",  # Cyrillic а homoglyph
        "assistant\u200b",  # trailing zero-width space
        "\u200bassistant",
        "wo\u200drk",
        "work\U0001f600",
        "\u05e2\u05d1\u05d5\u05d3\u05d4",  # Hebrew
        "\u202ework",  # RTL override
        "ｗｏｒｋ",  # fullwidth
        "work\r",
        "work\r\n",
        "\nwork",
        "work\x00",
        "WORK",
        "wOrk",
        "a" * 33,
        "a" * 100,
        " work",
        "work ",
    ],
)
def test_lane_rejects_unicode_case_whitespace_length(v, lane):
    bad(v, fr("msg.in", lane=lane))


@pytest.mark.parametrize("lane", [5, None, True, ["work"], {}])
def test_lane_wrong_json_type(v, lane):
    bad(v, fr("msg.in", lane=lane))


def test_lane_exactly_32_chars(v):
    ok(v, fr("msg.in", lane="a" * 32))
    ok(v, fr("msg.in", lane="z" + "9_-" * 10 + "x"))


@pytest.mark.parametrize("lane", ["\u0430ssistant", "Work", "a" * 33, "work\n", "w\u200bork"])
def test_lane_pattern_also_guards_chat_id_and_welcome_lanes(v, lane):
    bad(v, fr("msg.out", body=body("msg.out", chatId="glasses:" + lane)))
    bad(v, fr("welcome", body=body("welcome", lanes=[lane])))


def test_welcome_lanes_unique_and_bounded(v):
    bad(v, fr("welcome", body=body("welcome", lanes=["work", "work"])))
    ok(v, fr("welcome", body=body("welcome", lanes=[f"l{i}" for i in range(32)])))
    bad(v, fr("welcome", body=body("welcome", lanes=[f"l{i}" for i in range(33)])))


def test_contract_lone_surrogate_left_to_codec(v):
    # json.loads accepts "\ud800" and the schema cannot see it; re-encoding to UTF-8 then raises.
    # The codec (T1.06) must reject frames whose strings are not valid Unicode scalar values.
    inst = json.loads('{"text": "\\ud800", "origin": "ptt"}')
    ok(v, fr("msg.in", body=inst))
    with pytest.raises(UnicodeEncodeError):
        inst["text"].encode("utf-8")


def test_contract_nul_and_control_chars_pass_schema(v):
    ok(v, fr("msg.in", body={"text": "a\x00b\x1b[2J", "origin": "ptt"}))
