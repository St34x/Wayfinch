# SPDX-License-Identifier: Apache-2.0
"""The signer protocol where the brief is silent and the module docstring fixes the format: no whitespace around the
answer object, full-length PEM body lines, ``registry_revoked`` only ``true`` on ``revoke``, a coherent window, at
least one generated CRL; and requests that cannot be built outside the allowlist (direct ``Request`` construction,
text subclasses, non-mapping fields), with read-only result schemas."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from signer_vectors import BODY, DEVICE, H64, NULLS, RID, ROOT_ONLY_OPS, TIME, VALID, answer, error, expect, request

from glasses import signer_protocol as sp
from glasses.signer_protocol import AnswerNotJson, AnswerSchemaError


@pytest.mark.parametrize("wrap", ["crlf", "leading-space", "trailing-tab", "trailing-space"])
def test_an_answer_line_with_whitespace_around_the_object_is_refused(wrap: str) -> None:
    good = answer(VALID["status"])
    raw = {
        "crlf": good[:-1] + b"\r\n",
        "leading-space": b" " + good,
        "trailing-tab": good[:-1] + b"\t\n",
        "trailing-space": good[:-1] + b" \n",
    }[wrap]
    with pytest.raises(AnswerNotJson):
        sp.decode_answer(request("status"), raw)


def test_whitespace_inside_the_object_is_accepted() -> None:
    raw = answer(VALID["window_close"])
    assert sp.decode_answer(request("window_close"), b"{ \t" + raw[1:-2] + b" }\n") == VALID["window_close"]


def test_a_certificate_with_short_body_lines_is_refused() -> None:
    body = "".join(f"{BODY[i : i + 4]}\n" for i in range(0, len(BODY), 4))
    pem = f"-----BEGIN CERTIFICATE-----\n{body}-----END CERTIFICATE-----\n"
    expect(AnswerSchemaError, "enrol", answer({**VALID["enrol"], "cert": pem}))


@pytest.mark.parametrize("n_bytes", [99, 144], ids=["last-line-4", "last-line-64"])
def test_a_certificate_with_full_body_lines_and_a_shorter_last_line_is_accepted(n_bytes: int) -> None:
    body = base64.b64encode(bytes(range(n_bytes))).decode()
    pem = "-----BEGIN CERTIFICATE-----\n" + "".join(f"{body[i : i + 64]}\n" for i in range(0, len(body), 64))
    pem += "-----END CERTIFICATE-----\n"
    for cert in (pem, pem[:-1]):  # the final newline is optional
        assert sp.decode_answer(request("enrol"), answer({**VALID["enrol"], "cert": cert}))["cert"] == cert


def test_crl_failed_states_only_a_written_tombstone() -> None:
    expect(AnswerSchemaError, "revoke", error("crl_failed", registry_revoked=False))


def test_registry_revoked_comes_only_with_a_revoke_answer() -> None:
    assert expect(sp.SignerRefusal, "revoke", error("crl_failed")).code == "crl_failed"
    expect(AnswerSchemaError, "crl_generate", error("crl_failed", registry_revoked=True))
    expect(AnswerSchemaError, "status", error("crl_failed", registry_revoked=True))


@pytest.mark.parametrize(
    "change",
    [{"expires_at": None}, {"lanes": []}, {"name": None}, {"open": False}],
    ids=["expiry", "lanes", "name", "closed-with-expiry"],
)
def test_an_open_window_carries_its_expiry_name_and_lanes(change: dict[str, Any]) -> None:
    expect(AnswerSchemaError, "window_status", answer({**VALID["window_status"], **change}))


def test_a_pending_request_needs_an_open_window() -> None:
    closed = {**NULLS["window_status"], "pending": {"spki_sha256": H64, "since": TIME}}
    expect(AnswerSchemaError, "window_status", answer({**VALID["window_status"], **closed}))
    closed_status = {"open": False, "expires_at": None, "pending": True}
    expect(AnswerSchemaError, "status", answer({**VALID["status"], "window": closed_status}))


@pytest.mark.parametrize(
    "window",
    [{"open": True, "expires_at": None, "pending": False}, {"open": False, "expires_at": TIME, "pending": False}],
    ids=["open-without-expiry", "closed-with-expiry"],
)
def test_the_status_window_is_open_exactly_when_it_has_an_expiry(window: dict[str, Any]) -> None:
    expect(AnswerSchemaError, "status", answer({**VALID["status"], "window": window}))


def test_a_closed_window_with_its_name_and_lanes_kept_is_accepted() -> None:
    closed = {**VALID["window_status"], "open": False, "expires_at": None, "pending": None}
    assert sp.decode_answer(request("window_status"), answer(closed)) == closed
    status = {**VALID["status"], "window": {"open": False, "expires_at": None, "pending": False}}
    assert sp.decode_answer(request("status"), answer(status)) == status


def test_crl_generate_succeeds_only_with_at_least_one_crl() -> None:
    expect(AnswerSchemaError, "crl_generate", answer({"crl": []}))


def test_status_before_the_ca_exists_may_list_no_ca_and_no_crl() -> None:
    empty = {**VALID["status"], "ca": [], "crl": []}
    assert sp.decode_answer(request("status"), answer(empty)) == empty


@pytest.mark.parametrize("op", ROOT_ONLY_OPS)
def test_a_request_built_directly_for_a_root_only_op_is_unknown(op: str) -> None:
    with pytest.raises(sp.UnknownOp) as caught:
        sp.Request(op, RID, {})
    assert caught.value.__context__ is None


@pytest.mark.parametrize("rid", ["", RID.upper(), RID[:16], 7], ids=["empty", "upper", "short", "int"])
def test_a_request_built_directly_with_a_bad_rid_is_refused(rid: Any) -> None:
    with pytest.raises(sp.InvalidField):
        sp.Request("status", rid, {})


def test_a_request_built_directly_derives_its_line_and_checks_its_fields() -> None:
    assert sp.Request("revoke", RID, {"device_id": DEVICE}) == sp.build_request("revoke", RID, {"device_id": DEVICE})
    with pytest.raises(sp.InvalidField):
        sp.Request("revoke", RID, {"device_id": "x"})
    with pytest.raises(TypeError):
        sp.Request("status", RID, {}, line=b'{"v":1,"op":"window_open"}\n')  # type: ignore[call-arg]


@pytest.mark.parametrize("fields", ["device_id", 5, [("device_id", DEVICE)]], ids=["str", "int", "pairs"])
def test_request_fields_that_are_not_a_mapping_are_refused(fields: Any) -> None:
    with pytest.raises(sp.InvalidField):
        sp.build_request("revoke", RID, fields)


class _AlwaysEqual(str):
    """A text subclass that compares equal to every name, as a hostile caller in the same process could pass."""

    hashed: str

    def __new__(cls, text: str, hashed: str = "status") -> _AlwaysEqual:
        made = super().__new__(cls, text)
        made.hashed = hashed
        return made

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash(self.hashed)


def test_a_text_subclass_cannot_smuggle_a_root_only_op_into_the_line() -> None:
    with pytest.raises(sp.UnknownOp):
        sp.build_request(_AlwaysEqual("window_open"), RID)


def test_a_text_subclass_field_key_cannot_add_a_second_op() -> None:
    with pytest.raises(sp.InvalidField):
        sp.build_request("revoke", RID, {_AlwaysEqual("op", "device_id"): DEVICE})


def test_a_text_subclass_rid_or_field_value_is_refused() -> None:
    with pytest.raises(sp.InvalidField):
        sp.build_request("status", _AlwaysEqual(RID))
    with pytest.raises(sp.InvalidField):
        sp.build_request("revoke", RID, {"device_id": _AlwaysEqual(DEVICE)})


def test_the_result_schemas_are_read_only_at_every_level() -> None:
    with pytest.raises(TypeError):
        sp.RESULTS["enrol"]["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        sp.RESULTS["status"]["rotation"]["x"] = None  # type: ignore[index]
