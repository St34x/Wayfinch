# SPDX-License-Identifier: Apache-2.0
"""The signer wire protocol, on bytes: only the seven allowed ops can be built, every request field is
format-checked, and an answer that breaks framing, JSON strictness, the envelope or the error contract is refused
with a typed error that carries no input. Per-op result schemas: ``test_signer_protocol_results.py``."""

from __future__ import annotations

import json
from typing import Any

import pytest
from signer_vectors import (
    DEVICE,
    ENROL_FIELDS,
    NULLS,
    RID,
    ROOT_ONLY_OPS,
    VALID,
    answer,
    b64,
    b64url,
    error,
    expect,
    line,
    request,
)

from glasses import signer_protocol as sp
from glasses.signer_protocol import (
    AnswerNotJson,
    AnswerSchemaError,
    AnswerTooLarge,
    InvalidField,
    RequestTooLarge,
    RidMismatch,
    SignerRefusal,
    UnknownOp,
    build_request,
    decode_answer,
)

# --- requests ---------------------------------------------------------------------------------------------------


def test_the_op_allowlist_is_exactly_the_seven_ops_this_plugin_sends() -> None:
    assert {"status", "enrol", "revoke", "crl_generate", "window_status", "window_close", "rotate_status"} == sp.OPS
    assert set(sp.RESULTS) == sp.OPS


# `renew` is allowed for uid hermes by the brief but not built yet (T1.185), so it is refused like an unknown op.
@pytest.mark.parametrize("op", [*ROOT_ONLY_OPS, "renew", "", "STATUS", "status ", "Status", None, b"status", 1])
def test_root_only_unbuilt_unknown_or_non_text_op_names_are_refused(op: Any) -> None:
    with pytest.raises(UnknownOp):
        build_request(op, RID)


@pytest.mark.parametrize("rid", ["", RID[:-1], RID + "0", RID.upper(), "g" * 32, RID[:16], f" {RID[1:]}", None, 7])
def test_a_rid_that_is_not_32_lower_case_hex_is_refused(rid: Any) -> None:
    with pytest.raises(InvalidField):
        build_request("status", rid)


@pytest.mark.parametrize(
    ("op", "fields"),
    [
        ("status", {"device_id": DEVICE}),
        ("window_close", {"v": "2"}),
        ("revoke", {}),
        ("revoke", {"device_id": DEVICE, "reason": "lost"}),
        ("revoke", {"device_id": DEVICE.upper()}),
        ("revoke", {"device_id": DEVICE[:-1]}),
        ("revoke", {"device_id": b"00112233aabbccdd"}),
        *[("enrol", {k: v for k, v in ENROL_FIELDS.items() if k != name}) for name in ENROL_FIELDS],
        ("enrol", {**ENROL_FIELDS, "name": "x"}),
    ],
)
def test_missing_extra_or_malformed_request_fields_are_refused(op: str, fields: dict[str, Any]) -> None:
    with pytest.raises(InvalidField):
        build_request(op, RID, fields)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("spki", ""),
        ("spki", "AB=="),  # non-zero pad bits: not canonical
        ("spki", b64(bytes(91))[:-1]),  # padding missing
        ("spki", b64(bytes(30))[:8] + " " + b64(bytes(30))[8:]),
        ("spki", b64(b"\xfb\xff" * 30).replace("+", "-")),  # base64url alphabet
        ("spki", b64(bytes(257))),
        ("spki", 91),
        ("sig", b64(bytes(81))),
        ("code", b64url(bytes(16))[:-1]),
        ("code", b64url(bytes(17))),
        ("code", "B" * 22),  # non-zero trailing bits
        ("code", b64url(bytes(16))[:-1] + "+"),
        ("code", b64url(bytes(16)) + "=="),
    ],
)
def test_enrol_fields_outside_their_strict_base64_formats_are_refused(name: str, value: Any) -> None:
    with pytest.raises(InvalidField):
        build_request("enrol", RID, {**ENROL_FIELDS, name: value})


def test_enrol_accepts_the_largest_spki_and_signature() -> None:
    fields = {**ENROL_FIELDS, "spki": b64(bytes(256)), "sig": b64(bytes(80))}
    assert json.loads(build_request("enrol", RID, fields).line) == {"v": 1, "op": "enrol", "rid": RID, **fields}


def test_a_request_is_one_compact_json_line_and_its_repr_hides_the_fields() -> None:
    req = build_request("status", RID)
    assert req.line == b'{"v":1,"op":"status","rid":"' + RID.encode() + b'"}\n'
    assert build_request("status", RID, {}) == req
    enrol = request("enrol")
    assert ENROL_FIELDS["code"] not in repr(enrol) and ENROL_FIELDS["spki"] not in repr(enrol)


def test_a_request_line_over_16384_bytes_is_refused() -> None:
    padding = sp.REQUEST_CAP - len(b'{"pad":""}\n')
    assert len(sp._encode_line({"pad": "x" * padding})) == sp.REQUEST_CAP
    with pytest.raises(RequestTooLarge):
        sp._encode_line({"pad": "x" * (padding + 1)})


def test_caps_timeouts_and_public_file_names_match_the_contract() -> None:
    assert (sp.REQUEST_CAP, sp.ANSWER_CAP) == (16_384, 65_536)
    assert (sp.CONNECT_TIMEOUT_S, sp.OP_TIMEOUT_S, sp.ENROL_TIMEOUT_S) == (2.0, 10.0, 340.0)
    assert (sp.PUBLIC_CA_BUNDLE, sp.PUBLIC_CRL, sp.PUBLIC_REGISTRY) == (
        "device-ca.pem",
        "devices-crl.pem",
        "devices.json",
    )


# --- answers: framing and JSON ------------------------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted(sp.OPS))
def test_a_valid_answer_of_each_op_decodes_to_its_result(op: str) -> None:
    assert decode_answer(request(op), answer(VALID[op])) == VALID[op]


@pytest.mark.parametrize("op", sorted(NULLS))
def test_null_and_empty_values_are_accepted_where_the_schema_allows_them(op: str) -> None:
    result = {**VALID[op], **NULLS[op]}
    assert decode_answer(request(op), answer(result)) == result


def test_an_answer_over_65536_bytes_is_refused_and_one_at_the_cap_is_read() -> None:
    raw = answer(VALID["window_close"])
    at_cap = b"{" + b" " * (sp.ANSWER_CAP - len(raw)) + raw[1:]  # padded inside the object
    assert decode_answer(request("window_close"), at_cap) == VALID["window_close"]
    expect(AnswerTooLarge, "window_close", at_cap[:-1] + b" \n")


CLOSE = answer({"was_open": True})


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\n",
        CLOSE[:-1],  # no newline
        CLOSE[:-1] + b" ",  # a whole object, then no newline
        CLOSE + b"x",  # a byte after the newline
        CLOSE + CLOSE,
        CLOSE.replace(b",", b",\n", 1),
        CLOSE.replace(b'"rid": "', b'"rid": "\xff', 1),  # not UTF-8
        "\ufeff".encode() + CLOSE,
        CLOSE[:-1].decode().encode("utf-16") + b"\n",
        b"hello\n",
        b'[{"v": 1}]\n',
        b'"ok"\n',
        b"1\n",
        CLOSE.replace(b'"ok": true', b'"ok": true, "ok": true'),
        CLOSE.replace(b"true}", b'true, "was_open": false}'),
        line({"v": 1, "rid": RID, "ok": True, "was_open": {"a": 1}}).replace(b'"a": 1', b'"a": 1, "a": 2'),
        *[CLOSE.replace(b"true}", b"%s}" % c) for c in (b"NaN", b"Infinity", b"-Infinity")],
        b"[" * (sp.ANSWER_CAP - 1) + b"\n",
        CLOSE.replace(b"true}", b"1" + b"0" * 5000 + b"}"),
    ],
)
def test_an_answer_that_is_not_one_strict_json_object_line_is_refused(raw: bytes) -> None:
    expect(AnswerNotJson, "window_close", raw)


# --- answers: envelope -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "envelope",
    [
        {"rid": RID, "ok": True},
        {"v": 2, "rid": RID, "ok": True},
        {"v": True, "rid": RID, "ok": True},
        {"v": 1.0, "rid": RID, "ok": True},
        {"v": "1", "rid": RID, "ok": True},
        {"v": 1, "ok": True},
        {"v": 1, "rid": 1, "ok": True},
        {"v": 1, "rid": RID},
        {"v": 1, "rid": RID, "ok": "true"},
        {"v": 1, "rid": RID, "ok": 1},
        {"v": 1, "rid": RID, "ok": None},
        {"v": 1, "rid": RID, "ok": True, "error": "internal"},
    ],
)
def test_an_envelope_with_a_wrong_or_missing_v_rid_or_ok_is_refused(envelope: dict[str, Any]) -> None:
    expect(AnswerSchemaError, "window_close", line({**envelope, "was_open": True}))


@pytest.mark.parametrize("rid", [RID.upper(), "f" * 32, RID[:16], ""])
def test_an_answer_for_another_rid_is_refused(rid: str) -> None:
    expect(RidMismatch, "window_close", answer({"was_open": True}, rid=rid))
    expect(RidMismatch, "window_close", line({"v": 1, "rid": rid, "ok": False, "error": "internal"}))


# --- answers: error answers --------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(sp.ERROR_CODES))
def test_each_contract_error_code_raises_a_refusal_that_carries_it(code: str) -> None:
    refusal = expect(SignerRefusal, "status", error(code))
    assert isinstance(refusal, SignerRefusal)
    assert (refusal.code, refusal.retry_after_s, refusal.registry_revoked) == (code, None, None)


def test_the_error_codes_are_exactly_those_of_the_contract() -> None:
    contract = "bad_request forbidden enrol_refused bad_proof known_key unknown_device crl_failed not_initialised"
    assert set(f"{contract} exists rotation_state no_pending mismatch rate_limited internal".split()) == sp.ERROR_CODES


def test_the_two_allowed_extras_travel_on_the_refusal() -> None:
    limited = expect(SignerRefusal, "enrol", error("rate_limited", retry_after_s=30))
    failed = expect(SignerRefusal, "revoke", error("crl_failed", registry_revoked=True))
    assert isinstance(limited, SignerRefusal) and isinstance(failed, SignerRefusal)
    assert (limited.retry_after_s, limited.registry_revoked) == (30, None)
    assert (failed.retry_after_s, failed.registry_revoked) == (None, True)


@pytest.mark.parametrize(
    "raw",
    [
        error("teapot"),
        error("INTERNAL"),
        line({"v": 1, "rid": RID, "ok": False}),
        line({"v": 1, "rid": RID, "ok": False, "error": 1}),
        line({"v": 1, "rid": RID, "ok": False, "error": None}),
        error("internal", detail="disk full"),
        error("internal", device_id=DEVICE),
        error("internal", retry_after_s=30),
        error("bad_request", registry_revoked=True),
        error("rate_limited", registry_revoked=True),
        error("rate_limited", retry_after_s=30, registry_revoked=True),
        *[error("rate_limited", retry_after_s=bad) for bad in (-1, "30", True, 1.5, None)],
        *[error("crl_failed", registry_revoked=bad) for bad in ("yes", 1, None)],
    ],
)
def test_an_error_answer_outside_the_contract_is_refused(raw: bytes) -> None:
    expect(AnswerSchemaError, "revoke", raw)
