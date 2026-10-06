# SPDX-License-Identifier: Apache-2.0
"""Body sub-schema tests for protocol/envelope.schema.json: per-type negatives, approval/clarify (clarify cards
never expire, protocol/README.md "Card states"), card.state, skills and meetings. Shared helpers: test_schema.py.
"""

import copy

import pytest

from test_schema import DESIGN_TYPES, TS, ULID, VALID_BODIES, assert_invalid, assert_valid, frame

# --- body negatives -----------------------------------------------------------------------------


@pytest.mark.parametrize("type_", sorted(DESIGN_TYPES))
def test_extra_body_field_rejected(validator, type_):
    body = copy.deepcopy(VALID_BODIES[type_])
    body["unexpected"] = 1
    assert_invalid(validator, frame(type_, body=body))


@pytest.mark.parametrize("type_", sorted(t for t, b in VALID_BODIES.items() if b))
def test_body_of_other_type_rejected(validator, type_):
    # the body sub-schema is selected by `type`
    other = "ack" if type_ != "ack" else "skill.cancel"
    assert_invalid(validator, frame(type_, body=VALID_BODIES[other]))


def test_msg_in_text_limit(validator):
    assert_valid(validator, frame("msg.in", body={"text": "x" * 2000, "origin": "wake"}))
    assert_invalid(validator, frame("msg.in", body={"text": "x" * 2001, "origin": "wake"}))
    assert_invalid(validator, frame("msg.in", body={"text": "", "origin": "wake"}))
    assert_invalid(validator, frame("msg.in", body={"text": "hi", "origin": "shout"}))


def test_audio_limits(validator):
    assert_valid(validator, frame("audio.end", body={"clipId": ULID, "durationMs": 30000}))
    assert_invalid(validator, frame("audio.end", body={"clipId": ULID, "durationMs": 30001}))
    assert_invalid(validator, frame("audio.start", body={"clipId": ULID, "mime": "audio/wav"}))
    assert_invalid(validator, frame("audio.chunk", body={"clipId": ULID, "chunk": "A" * 32772}))


@pytest.mark.parametrize("chunk", ["not base64!", "AAA", "AA=A", "A===", "AAAA\n"])
def test_bad_base64_rejected(validator, chunk):
    assert_invalid(validator, frame("audio.chunk", body={"clipId": ULID, "chunk": chunk}))
    assert_invalid(
        validator,
        frame("meeting.audio", body={"meetingId": ULID, "seq": 1, "mime": "audio/ogg;codecs=opus", "chunk": chunk}),
    )


@pytest.mark.parametrize("chunk", ["AAAA", "AAA=", "AA==", "T2dnUw=="])
def test_good_base64_accepted(validator, chunk):
    assert_valid(validator, frame("audio.chunk", body={"clipId": ULID, "chunk": chunk}))


def test_msg_out_variants(validator):
    body = dict(VALID_BODIES["msg.out"])
    body.update(
        kind="answer",
        longRef={"id": ULID, "channel": "telegram", "profile": "work", "status": "pending", "fullText": "x" * 4096},
    )
    assert_valid(validator, frame("msg.out", body=body))
    body["longRef"] = dict(body["longRef"], fullText="x" * 4097)
    assert_invalid(validator, frame("msg.out", body=body))
    notice = dict(VALID_BODIES["msg.out"], kind="notice", notice={"kind": "kanban_done", "ref": "t_123"})
    assert_valid(validator, frame("msg.out", body=notice))
    no_notice = dict(VALID_BODIES["msg.out"], kind="notice")
    assert_invalid(validator, frame("msg.out", body=no_notice))
    # notice only with kind `notice`
    stray = dict(VALID_BODIES["msg.out"], notice={"kind": "system"})
    assert_invalid(validator, frame("msg.out", body=stray))
    assert_invalid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], kind="shout")))
    assert_invalid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], chatId="telegram:assistant")))
    assert_invalid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], chatId="glasses:assistant\n")))


LINE_TERMINATOR_GUARD = {"pattern": "[\\n\\r\\u0085\\u2028\\u2029]"}


def test_trailing_newline_never_matches_an_anchored_pattern(schema):
    # Python re lets `$` match before a final \n; java.util.regex (no UNIX_LINES) also before \r, \r\n, \u0085,
    # \u2028, \u2029. Every `$`-anchored pattern carries a guard against all of them.
    out = []

    def walk(node, path):
        if isinstance(node, dict):
            anchored = isinstance(node.get("pattern"), str) and node["pattern"].endswith("$")
            if anchored and node.get("not") != LINE_TERMINATOR_GUARD:
                out.append(path)
            for k, v in node.items():
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for n, v in enumerate(node):
                walk(v, f"{path}/{n}")

    walk(schema, "#")
    assert not out, out


def test_approval_request_limits(validator):
    ok = dict(VALID_BODIES["approval.request"], summary="x" * 300, choices=["deny"])
    assert_valid(validator, frame("approval.request", body=ok))
    assert_invalid(validator, frame("approval.request", body=dict(ok, summary="x" * 301)))
    assert_invalid(validator, frame("approval.request", body=dict(ok, choices=["always"])))
    assert_invalid(validator, frame("approval.request", body=dict(ok, choices=[])))
    assert_invalid(validator, frame("approval.request", body=dict(ok, choices=["once", "once"])))
    no_expiry = dict(ok)
    del no_expiry["expiresAt"]
    assert_invalid(validator, frame("approval.request", body=no_expiry))


# --- clarify never expires (protocol/README.md "Card states") -----------------------------------


def test_clarify_request_has_detached_and_no_expiry(validator):
    base = VALID_BODIES["clarify.request"]
    assert_valid(validator, frame("clarify.request", body=dict(base, detached=True)))
    assert_valid(validator, frame("clarify.request", body=dict(base, choices=[])))
    assert_invalid(validator, frame("clarify.request", body=dict(base, expiresAt=TS)))
    assert_invalid(validator, frame("clarify.request", body=dict(base, choices=["a", "b", "c", "d", "e"])))
    no_detached = dict(base)
    del no_detached["detached"]
    assert_invalid(validator, frame("clarify.request", body=no_detached))


def test_clarify_response_answer_xor_dismiss(validator):
    assert_valid(validator, frame("clarify.response", body={"clarifyId": "clr-1", "dismiss": True}))
    assert_invalid(validator, frame("clarify.response", body={"clarifyId": "clr-1", "dismiss": False}))
    assert_invalid(validator, frame("clarify.response", body={"clarifyId": "clr-1", "answer": "A", "dismiss": True}))
    assert_invalid(validator, frame("clarify.response", body={"clarifyId": "clr-1"}))


@pytest.mark.parametrize("state", ["answered", "answered_elsewhere", "detached", "dismissed"])
def test_card_state_clarify_states(validator, state):
    assert_valid(validator, frame("card.state", body={"clarifyId": "clr-1", "state": state}))


def test_card_state_clarify_never_expires(validator):
    assert_invalid(validator, frame("card.state", body={"clarifyId": "clr-1", "state": "expired"}))


@pytest.mark.parametrize("state", ["answered", "answered_elsewhere", "expired"])
def test_card_state_approval_states(validator, state):
    assert_valid(validator, frame("card.state", body={"approvalId": "a1", "state": state}))


@pytest.mark.parametrize("state", ["detached", "dismissed"])
def test_card_state_approval_never_detached(validator, state):
    assert_invalid(validator, frame("card.state", body={"approvalId": "a1", "state": state}))


def test_card_state_needs_exactly_one_id(validator):
    assert_invalid(validator, frame("card.state", body={"state": "answered"}))
    assert_invalid(validator, frame("card.state", body={"approvalId": "a1", "clarifyId": "c1", "state": "answered"}))


# --- skills and meetings ------------------------------------------------------------------------


def test_skill_call_constraints(validator):
    base = VALID_BODIES["skill.call"]
    assert_invalid(validator, frame("skill.call", body=dict(base, skill="Audio")))
    assert_invalid(validator, frame("skill.call", body=dict(base, mode="write")))
    assert_invalid(validator, frame("skill.call", body=dict(base, deadlineMs=0)))
    assert_invalid(validator, frame("skill.result", body={"callId": ULID, "status": "maybe"}))


def test_meeting_constraints(validator):
    hint = VALID_BODIES["meeting.hint"]
    assert_valid(validator, frame("meeting.hint", body=dict(hint, text="x" * 180, ttlMs=10000)))
    assert_invalid(validator, frame("meeting.hint", body=dict(hint, text="x" * 181)))
    start = VALID_BODIES["meeting.start"]
    assert_valid(validator, frame("meeting.start", body=dict(start, prepTerms=["t"] * 50)))
    assert_invalid(validator, frame("meeting.start", body=dict(start, prepTerms=["t"] * 51)))
    assert_invalid(validator, frame("meeting.start", body=dict(start, meetingId="not-a-ulid")))
    ctl = {"meetingId": ULID, "action": "hint_level"}
    assert_invalid(validator, frame("meeting.control", body=ctl))
    assert_valid(validator, frame("meeting.control", body=dict(ctl, hintLevel="low")))
    st = dict(VALID_BODIES["meeting.state"], state="error")
    assert_invalid(validator, frame("meeting.state", body=st))
    assert_valid(validator, frame("meeting.state", body=dict(st, error="provider_down")))
