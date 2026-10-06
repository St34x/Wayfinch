# SPDX-License-Identifier: Apache-2.0
"""Adversarial tests for protocol/envelope.schema.json (T1.02): ULIDs, seq and integer bounds, base64,
clarify expiry fields. Conventions and helpers: test_schema_adversarial.py.
"""

import pytest

from test_schema import TS, ULID
from test_schema_adversarial import bad, body, fr, ok

# --- invalid ULIDs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "8" + "0" * 25,  # timestamp overflow (first char > 7)
        "Z" + "0" * 25,
        "01jabcdefghjkmnpqrstvwxyz0",  # lowercase
        "01JABCDEFGHJKMNPQRSTVWXYZU",  # U not in Crockford
        "01JABCDEFGHJKMNPQRSTVWXYZI",
        "01JABCDEFGHJKMNPQRSTVWXYZL",
        "01JABCDEFGHJKMNPQRSTVWXYZO",
        ULID[:25],
        ULID + "0",
        " " + ULID[1:],
        ULID[:-1] + " ",
        "\uff10" + ULID[1:],  # fullwidth zero
        "\u0661" + ULID[1:],  # Arabic-Indic digit one
        "01JABCDEFGHJKMNPQRSTVWXY-0",
        ULID + "\n",
    ],
)
def test_invalid_ulid_rejected_in_every_ulid_slot(v, bad_id):
    bad(v, fr("ping", id=bad_id))
    bad(v, fr("ping", corr=bad_id))
    bad(v, fr("meeting.control", body={"meetingId": bad_id, "action": "pause"}))


@pytest.mark.parametrize("bad_id", [None, 1, 0x01, ["x"], {}])
def test_ulid_wrong_json_type(v, bad_id):
    bad(v, fr("ping", id=bad_id))


def test_ulid_extremes_accepted(v):
    ok(v, fr("ping", id="0" * 26))
    ok(v, fr("ping", id="7" + "Z" * 25))


# --- seq / integers -----------------------------------------------------------------------------


@pytest.mark.parametrize("seq", [0, -1, -(2**63), 2**53, 2**64, 10**30, 1.5, "7", None, True, [1]])
def test_seq_bad_values_rejected(v, seq):
    bad(v, fr("msg.out", seq=seq))


def test_seq_max_safe_integer_accepted(v):
    ok(v, fr("msg.out", seq=2**53 - 1))
    ok(v, fr("msg.out", seq=1))


def test_contract_seq_integral_float_accepted(v):
    # JSON Schema: 1.0 is an integer. A strict codec on the Kotlin side may differ; pin the schema behaviour.
    ok(v, fr("msg.out", seq=1.0))
    ok(v, fr("ping", v=1.0))
    bad(v, fr("ping", v=True))


@pytest.mark.parametrize(
    "t,field,lo,hi",
    [
        ("ack", "upToSeq", 0, 2**53 - 1),
        ("hello", "lastAckedSeq", 0, 2**53 - 1),
        ("welcome", "resumeFrom", 0, 2**53 - 1),
        ("meeting.audio", "seq", 0, 2**53 - 1),
        ("audio.end", "durationMs", 0, 30000),
        ("skill.call", "deadlineMs", 1, 600000),
        ("hud.state", "pendingApprovals", 0, 65535),
        ("hud.state", "unreadPushes", 0, 65535),
        ("meeting.state", "elapsedMs", 0, 86400000),
        ("meeting.hint", "ttlMs", 1000, 60000),
        ("hello", "protocol", 1, 255),
    ],
)
def test_integer_bounds(v, t, field, lo, hi):
    ok(v, fr(t, body=body(t, **{field: lo})))
    ok(v, fr(t, body=body(t, **{field: hi})))
    bad(v, fr(t, body=body(t, **{field: lo - 1})))
    bad(v, fr(t, body=body(t, **{field: hi + 1})))
    bad(v, fr(t, body=body(t, **{field: str(lo)})))
    bad(v, fr(t, body=body(t, **{field: None})))
    bad(v, fr(t, body=body(t, **{field: lo + 0.5})))


# --- base64 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunk",
    [
        "",
        "A",
        "AA",
        "AAA",
        "AAAAA",
        "AA=",
        "A==",
        "A===",
        "====",
        "AA==AAAA",  # padding in the middle
        "AAA=AAAA",
        "AAAAAA=",
        "AAAAA===",
        "AA-_",  # base64url alphabet
        "AA AA",
        "AAAA\r\n",
        "AAAA\n",
        "\nAAAA",
        "AA\u00c0A",
        "ＡＡＡＡ",
        "A" * 31748,
        "A" * 32772,
    ],
)
def test_base64_bad_padding_and_alphabet_rejected(v, chunk):
    bad(v, fr("audio.chunk", body={"clipId": "c1", "chunk": chunk}))
    bad(v, fr("meeting.audio", body=body("meeting.audio", chunk=chunk)))


def test_base64_at_limit(v):
    ok(v, fr("audio.chunk", body={"clipId": "c1", "chunk": "A" * 31744}))
    ok(v, fr("audio.chunk", body={"clipId": "c1", "chunk": "A" * 31740 + "AA=="}))
    bad(v, fr("audio.chunk", body={"clipId": "c1", "chunk": "A" * 31744 + "AA=="}))


def test_contract_non_canonical_base64_padding_bits_accepted(v):
    # "AB==" has non-zero trailing bits; strict decoders (Python validate=True does not check them, Java's
    # Base64 does not either) accept it. Pinned: the pattern only checks alphabet and padding shape.
    ok(v, fr("audio.chunk", body={"clipId": "c1", "chunk": "AB=="}))


# --- clarify.request with expiry fields (clarify never expires, protocol/README.md "Card states") ---


@pytest.mark.parametrize(
    "field,value",
    [
        ("expiresAt", TS),
        ("expiresAt", None),
        ("expiresAt", ""),
        ("expires_at", TS),
        ("expiry", TS),
        ("timeout", 180),
        ("timeoutMs", 180000),
        ("ttlMs", 10000),
        ("deadlineMs", 1000),
    ],
)
def test_clarify_request_any_expiry_field_rejected(v, field, value):
    bad(v, fr("clarify.request", body=body("clarify.request", **{field: value})))


@pytest.mark.parametrize("detached", [None, 0, 1, "true", "false"])
def test_clarify_detached_must_be_boolean(v, detached):
    bad(v, fr("clarify.request", body=body("clarify.request", detached=detached)))


@pytest.mark.parametrize(
    "b",
    [
        {"clarifyId": "c", "dismiss": True, "answer": ""},
        {"clarifyId": "c", "dismiss": "true"},
        {"clarifyId": "c", "dismiss": 1},
        {"clarifyId": "c", "dismiss": None},
        {"clarifyId": "c", "answer": None},
        {"clarifyId": "c", "answer": ""},
        {"clarifyId": "c", "answer": "a" * 2001},
        {"answer": "A"},
        {"clarifyId": "c", "answer": "A", "expiresAt": TS},
    ],
)
def test_clarify_response_invalid(v, b):
    bad(v, fr("clarify.response", body=b))
