# SPDX-License-Identifier: Apache-2.0
"""Edge and failure cases of the signer wire protocol: byte-exact caps, broken encodings, JSON corner cases, rid
types, op-name lookalikes and secrecy of the enrolment code."""

from __future__ import annotations

import base64
import json
import traceback
from typing import Any

import pytest
from signer_vectors import (
    BODY,
    DEVICE,
    ENROL_FIELDS,
    PEM,
    RID,
    ROOT_ONLY_OPS,
    TIME,
    VALID,
    answer,
    error,
    expect,
    line,
    request,
)

from glasses import signer_protocol as sp
from glasses.signer_protocol import AnswerNotJson, AnswerRejected, AnswerSchemaError, AnswerTooLarge, RidMismatch


def _status_of_exact_size(size: int) -> bytes:
    """A valid ``status`` answer line of exactly ``size`` bytes (newline included), padded via CA subject names."""
    entry = {"ca_id": DEVICE, "subject_cn": "a", "not_after": TIME, "role": "previous"}
    count = 1
    while True:
        cas = [dict(entry) for _ in range(count)]
        raw = answer({**VALID["status"], "ca": cas})
        missing = size - len(raw)
        if missing <= 255 * count:
            break
        count += 1
    for ca in cas:
        grow = min(missing, 255)
        ca["subject_cn"] += "a" * grow
        missing -= grow
    raw = answer({**VALID["status"], "ca": cas})
    assert len(raw) == size
    return raw


def test_an_answer_of_exactly_65536_bytes_with_its_newline_is_read() -> None:
    raw = _status_of_exact_size(sp.ANSWER_CAP)
    cas = sp.decode_answer(request("status"), raw)["ca"]
    assert isinstance(cas, list) and len(cas) > 1


def test_an_answer_one_byte_over_the_cap_is_too_large_though_valid_json() -> None:
    raw = _status_of_exact_size(sp.ANSWER_CAP + 1)
    expect(AnswerTooLarge, "status", raw)


def test_an_answer_at_the_cap_without_its_newline_is_not_a_line() -> None:
    raw = _status_of_exact_size(sp.ANSWER_CAP + 1)[:-1]
    assert len(raw) == sp.ANSWER_CAP
    expect(AnswerNotJson, "status", raw)


def test_a_request_line_of_exactly_16384_bytes_is_built_and_one_byte_more_is_refused() -> None:
    filler = sp.REQUEST_CAP - len(sp._encode_line({"x": ""}))
    assert len(sp._encode_line({"x": "a" * filler})) == sp.REQUEST_CAP
    with pytest.raises(sp.RequestTooLarge) as caught:
        sp._encode_line({"x": "a" * (filler + 1)})
    assert str(caught.value) == sp.RequestTooLarge.message


def test_the_largest_enrol_request_stays_far_below_the_request_cap() -> None:
    fields = {"spki": base64.b64encode(bytes(256)).decode(), "code": ENROL_FIELDS["code"]}
    fields["sig"] = base64.b64encode(bytes(80)).decode()
    assert len(sp.build_request("enrol", RID, fields).line) < sp.REQUEST_CAP // 16


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",  # never valid in UTF-8
        b"\xc0\xaf",  # overlong encoding of "/"
        b"\xed\xa0\x80",  # a UTF-16 surrogate encoded as UTF-8
        b"\xe2\x82",  # truncated multi-byte sequence
    ],
    ids=["ff", "overlong", "encoded-surrogate", "truncated"],
)
def test_non_utf8_bytes_inside_a_string_are_refused(raw: bytes) -> None:
    good = answer(VALID["status"])
    expect(AnswerNotJson, "status", good.replace(b'"1.1.0"', b'"' + raw + b'"'))


@pytest.mark.parametrize("key", ["signer_version", "subject_cn"])
def test_a_lone_surrogate_escape_in_a_text_field_is_refused(key: str) -> None:
    result: dict[str, Any] = json.loads(json.dumps(VALID["status"]))
    target = result if key == "signer_version" else result["ca"][0]
    target[key] = "ok\ud800"
    expect(AnswerSchemaError, "status", answer(result))


def test_a_lone_surrogate_escape_in_a_window_name_is_refused() -> None:
    expect(AnswerSchemaError, "window_status", answer({**VALID["window_status"], "name": "Pixel\udfff"}))


def test_a_lone_surrogate_escape_in_the_rid_or_error_code_is_refused() -> None:
    expect(RidMismatch, "status", answer(VALID["status"], rid=RID[:-1] + "\ud800"))
    expect(AnswerSchemaError, "status", error("internal\ud800"))


@pytest.mark.parametrize(
    ("op", "old", "new"),
    [
        ("status", b'"v": 1,', b'"v": 1, "v": 1,'),
        ("status", b'"ok": true,', b'"ok": true, "ok": true,'),
        ("status", b'"rid": ', b'"rid": "' + RID.encode() + b'", "rid": '),
        ("status", b'"pending": false}', b'"pending": false, "pending": false}'),
        ("status", b'"role": "current"}', b'"role": "current", "role": "previous"}'),
        ("revoke", b'"crl_number": 8}', b'"crl_number": 8, "crl_number": 8}'),
    ],
    ids=["v", "ok", "rid", "nested-same-value", "in-list-item", "result-same-value"],
)
def test_a_duplicate_key_at_any_depth_is_refused_also_with_the_same_value(op: str, old: bytes, new: bytes) -> None:
    good = answer(VALID[op])
    assert good.count(old) == 1
    expect(AnswerNotJson, op, good.replace(old, new))


def test_a_duplicate_key_in_an_error_answer_is_refused() -> None:
    expect(AnswerNotJson, "status", error("internal").replace(b'"error"', b'"error": "internal", "error"'))


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
@pytest.mark.parametrize("old", [b'"crl_number": 8', b'"already_revoked": false', b'"v": 1'])
def test_nan_and_infinity_are_refused_in_any_value(constant: bytes, old: bytes) -> None:
    good = answer(VALID["revoke"])
    expect(AnswerNotJson, "revoke", good.replace(old, old.split(b":")[0] + b": " + constant))


@pytest.mark.parametrize("number", [b"1e400", b"-1e400", b"8.0", b"8e0", b"-0.0"])
def test_a_float_where_a_count_belongs_is_refused(number: bytes) -> None:
    expect(AnswerSchemaError, "revoke", answer(VALID["revoke"]).replace(b'"crl_number": 8', b'"crl_number": ' + number))


def test_an_integer_too_long_to_parse_is_a_typed_refusal() -> None:
    raw = answer(VALID["revoke"]).replace(b'"crl_number": 8', b'"crl_number": 1' + b"0" * 5000)
    expect(AnswerNotJson, "revoke", raw)


@pytest.mark.parametrize(("value", "accepted"), [(2**63 - 1, True), (2**63, False), (-1, False), (0, True)])
def test_counts_stop_at_two_to_the_63(value: int, accepted: bool) -> None:
    raw = answer({**VALID["revoke"], "crl_number": value})
    if accepted:
        assert sp.decode_answer(request("revoke"), raw)["crl_number"] == value
    else:
        expect(AnswerSchemaError, "revoke", raw)


@pytest.mark.parametrize("value", [-1, 2**63, 1.5, True, "5", None])
def test_retry_after_outside_a_non_negative_int_is_refused(value: Any) -> None:
    expect(AnswerSchemaError, "status", error("rate_limited", retry_after_s=value))


@pytest.mark.parametrize("depth", [1000, 20000])
def test_deeply_nested_json_within_the_cap_is_a_typed_refusal(depth: int) -> None:
    with pytest.raises(AnswerRejected) as caught:
        sp.decode_answer(request("status"), b"[" * depth + b"]" * depth + b"\n")
    assert caught.value.__context__ is None
    nested = b'{"a":' * depth + b"1" + b"}" * depth
    raw = answer(VALID["window_close"])[:-2] + b', "x": ' + nested + b"}\n"
    with pytest.raises(AnswerRejected):
        sp.decode_answer(request("window_close"), raw)


def test_a_utf8_byte_order_mark_before_the_object_is_refused() -> None:
    expect(AnswerNotJson, "status", b"\xef\xbb\xbf" + answer(VALID["status"]))


@pytest.mark.parametrize("raw", [b"\n", b"", b"\r\n", b" \n", b"null\n", b'"x"\n', b"[]\n", b"{}{}\n"])
def test_an_empty_or_non_object_line_is_refused(raw: bytes) -> None:
    expect(AnswerNotJson, "status", raw)


@pytest.mark.parametrize(
    "rid",
    [123, None, True, [RID], {"rid": RID}, 1.5],
    ids=["int", "null", "bool", "list", "object", "float"],
)
def test_a_rid_that_is_not_a_string_is_a_schema_error(rid: Any) -> None:
    expect(AnswerSchemaError, "status", line({"v": 1, "rid": rid, "ok": True, **VALID["status"]}))


@pytest.mark.parametrize(
    "rid",
    [RID.upper(), RID[:16], RID + "0", " " + RID, RID + "\n", RID[:-1] + "\u0430", ""],
    ids=["upper", "prefix-16", "longer", "space", "newline", "cyrillic-a", "empty"],
)
def test_a_rid_string_other_than_the_exact_echo_is_a_mismatch(rid: str) -> None:
    expect(RidMismatch, "status", answer(VALID["status"], rid=rid))


def test_an_error_answer_for_another_rid_is_a_mismatch_not_a_refusal() -> None:
    raw = line({"v": 1, "rid": "f" * 32, "ok": False, "error": "enrol_refused"})
    expect(RidMismatch, "enrol", raw)


def _variants(name: str) -> list[str]:
    """Spellings of ``name`` that differ from it: casing, surrounding whitespace, separators, lookalike letters."""
    spellings = [
        name.upper(),
        name.title(),
        f" {name}",
        f"{name} ",
        f"\t{name}",
        f"{name}\n",
        f"{name}\x00",
        name.replace("_", "-"),
        name.replace("_", ""),
        f"\ufeff{name}",
        name.replace("o", "\u043e"),
        name.replace("s", "\u017f"),
    ]
    return list(dict.fromkeys(spelling for spelling in spellings if spelling != name))


@pytest.mark.parametrize("op", [variant for name in ROOT_ONLY_OPS for variant in [name, *_variants(name)]])
def test_a_root_only_op_in_any_casing_or_with_whitespace_is_unknown(op: str) -> None:
    with pytest.raises(sp.UnknownOp) as caught:
        sp.build_request(op, RID, {})
    assert str(caught.value) == sp.UnknownOp.message and caught.value.__context__ is None


@pytest.mark.parametrize("op", [variant for name in sorted(sp.OPS) for variant in _variants(name)])
def test_an_allowed_op_name_is_matched_only_exactly(op: str) -> None:
    with pytest.raises(sp.UnknownOp):
        sp.build_request(op, RID, {})


def test_no_root_only_op_matches_the_allowlist_case_insensitively() -> None:
    assert not {name.casefold() for name in ROOT_ONLY_OPS} & {name.casefold() for name in sp.OPS}
    assert not any(
        name.startswith(("window_open", "window_confirm", "ca_", "rotate_")) for name in sp.OPS - {"rotate_status"}
    )


def _exception_text(exc: BaseException) -> str:
    return "".join([str(exc), repr(exc), repr(exc.args), *traceback.format_exception(exc)])


def test_the_enrol_code_never_appears_in_a_request_repr_or_str() -> None:
    code = ENROL_FIELDS["code"]
    req = request("enrol")
    for text in (repr(req), str(req), f"{req}", f"{req!r}", format(req, "")):
        assert code not in text and "fields" not in text
    assert code.encode() in req.line  # it is sent, only never printed


@pytest.mark.parametrize("change", ["short", "long", "padded", "plus", "space"])
def test_a_refused_enrol_request_never_echoes_its_code(change: str) -> None:
    code = ENROL_FIELDS["code"]
    bad = {
        "short": code[:-1],
        "long": code + "A",
        "padded": code + "==",
        "plus": code.replace(code[0], "+", 1),
        "space": code + " ",
    }[change]
    with pytest.raises(sp.InvalidField) as caught:
        sp.build_request("enrol", RID, {**ENROL_FIELDS, "code": bad})
    assert bad not in _exception_text(caught.value) and caught.value.__context__ is None


def test_an_answer_echoing_the_enrol_code_is_refused_without_echoing_it() -> None:
    code = ENROL_FIELDS["code"]
    for raw in (
        answer({**VALID["enrol"], "code": code}),
        answer({**VALID["enrol"], "token": code}),
        line({"v": 1, "rid": RID, "ok": False, "error": "enrol_refused", "code": code}),
        error(code),
        answer(VALID["enrol"], rid=code),
        answer(VALID["enrol"])[:-2] + b', "' + code.encode() + b'": NaN}\n',
    ):
        with pytest.raises(AnswerRejected) as caught:
            sp.decode_answer(request("enrol"), raw)
        assert code not in _exception_text(caught.value)


def test_a_signer_refusal_carries_only_its_code_and_extras() -> None:
    with pytest.raises(sp.SignerRefusal) as caught:
        sp.decode_answer(request("enrol"), error("enrol_refused"))
    assert str(caught.value) == sp.SignerRefusal.message and ENROL_FIELDS["code"] not in _exception_text(caught.value)
    assert (caught.value.code, caught.value.retry_after_s, caught.value.registry_revoked) == (
        "enrol_refused",
        None,
        None,
    )


def test_a_certificate_body_line_of_65_characters_is_refused() -> None:
    lines = [BODY[:65], *(BODY[start : start + 64] for start in range(65, len(BODY), 64))]
    assert len(lines[0]) == 65 and all(len(part) <= 64 for part in lines[1:])
    body = "".join(f"{part}\n" for part in lines)
    pem = f"-----BEGIN CERTIFICATE-----\n{body}-----END CERTIFICATE-----\n"
    expect(AnswerSchemaError, "enrol", answer({**VALID["enrol"], "cert": pem}))


def test_two_certificates_or_crlf_line_ends_in_the_cert_are_refused() -> None:
    expect(AnswerSchemaError, "enrol", answer({**VALID["enrol"], "cert": PEM + PEM}))
    expect(AnswerSchemaError, "enrol", answer({**VALID["enrol"], "cert": PEM.replace("\n", "\r\n")}))
