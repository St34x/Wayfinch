# SPDX-License-Identifier: Apache-2.0
"""Per-op answer schemas of the signer protocol: every result key is required, no object takes an extra key, every
value has its type and format (hex ids, RFC 3339 UTC times, enums, counts, PEM, base64url token, window names and
lanes), and a ``revoke`` answer must name the device it was asked about."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from signer_vectors import (
    BODY,
    DEVICE,
    H64,
    PEM,
    VALID,
    answer,
    at,
    b64,
    b64url,
    changed,
    expect,
    ident,
    object_paths,
    request,
    value_paths,
)

from glasses import signer_protocol as sp
from glasses.signer_protocol import AnswerSchemaError, decode_answer

KEYS = [(op, (*obj, key)) for op in sorted(sp.OPS) for obj in object_paths(VALID[op]) for key in at(VALID[op], obj)]
OBJECTS = [(op, path) for op in sorted(sp.OPS) for path in object_paths(VALID[op])]
VALUES = [(op, path) for op in sorted(sp.OPS) for path in value_paths(VALID[op])]
FREE_TEXT = ("subject_cn", "signer_version", "name")
NULLABLE = ("expires_at", "last_attempt_at", "name", "pending")
LISTS = ("ca", "crl", "installed", "lanes", "devices_on_previous")


@pytest.mark.parametrize(("op", "path"), KEYS, ids=ident(KEYS))
def test_an_answer_missing_any_result_key_is_refused(op: str, path: tuple[Any, ...]) -> None:
    expect(AnswerSchemaError, op, answer(changed(op, path, drop=True)))


@pytest.mark.parametrize(("op", "path"), OBJECTS, ids=ident(OBJECTS))
def test_an_answer_with_an_extra_key_in_any_object_is_refused(op: str, path: tuple[Any, ...]) -> None:
    result = copy.deepcopy(VALID[op])
    at(result, path)["unexpected"] = 1
    expect(AnswerSchemaError, op, answer(result))


@pytest.mark.parametrize(("op", "path"), VALUES, ids=ident(VALUES))
@pytest.mark.parametrize(
    "wrong", [1.5, {"unexpected": 1}, "X", [], None], ids=["float", "object", "text", "list", "null"]
)
def test_a_result_value_of_the_wrong_type_is_refused(op: str, path: tuple[Any, ...], wrong: Any) -> None:
    allowed = FREE_TEXT if wrong == "X" else NULLABLE if wrong is None else LISTS if wrong == [] else ()
    if path[-1] not in allowed:  # where the schema allows the value, it is covered by the valid-answer tests
        expect(AnswerSchemaError, op, answer(changed(op, path, wrong)))


BAD_TIMES = [
    "2026-10-11T12:00:00+00:00",
    "2026-10-11 12:00:00Z",
    "2026-10-11T12:00:00.5Z",
    "2026-10-11T12:00Z",
    "2026-13-01T00:00:00Z",
    "2026-02-30T00:00:00Z",
    "2026-10-11T24:00:00Z",
    "2026-10-11T12:00:60Z",
    "0000-01-01T00:00:00Z",
    "\uff12026-10-11T12:00:00Z",  # a full-width digit
    "2026-10-11t12:00:00z",
]
FORMAT_CASES = [
    ("status", ("ca", 0, "ca_id"), DEVICE.upper()),
    ("status", ("ca", 0, "ca_id"), DEVICE[:-1]),
    ("status", ("ca", 0, "role"), "root"),
    ("status", ("ca", 0, "subject_cn"), ""),
    ("status", ("ca", 0, "subject_cn"), "a\x00b"),
    ("status", ("ca", 0, "subject_cn"), "\ud800"),
    ("status", ("ca", 0, "subject_cn"), "x" * 257),
    *[("status", ("ca", 0, "not_after"), bad) for bad in BAD_TIMES],
    ("status", ("crl", 0, "number"), -1),
    ("status", ("crl", 0, "number"), True),
    ("status", ("crl", 0, "number"), 2**63),
    ("status", ("crl", 0, "number"), 7.0),
    ("status", ("crl", 0, "sha256"), H64[:-1]),
    ("status", ("edge", "state"), "fine"),
    ("status", ("edge", "last_attempt_at"), ""),
    ("status", ("window", "open"), 1),
    ("status", ("rotation", "state"), "begun"),
    ("enrol", ("cert",), PEM + PEM),
    ("enrol", ("cert",), PEM.replace("CERTIFICATE", "PRIVATE KEY")),
    ("enrol", ("cert",), PEM.replace("\n", "\r\n")),
    ("enrol", ("cert",), PEM.replace("-----END CERTIFICATE-----\n", "")),
    ("enrol", ("cert",), PEM.replace(BODY[:4], "A===", 1)),
    ("enrol", ("cert",), PEM.replace(BODY[:64], BODY[:65], 1)),
    ("enrol", ("cert",), "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n"),
    ("enrol", ("token",), b64url(bytes(32))[:-1]),
    ("enrol", ("token",), b64url(bytes(33))),
    ("enrol", ("token",), "B" * 43),
    ("enrol", ("token",), b64(bytes(range(32)))),
    ("window_status", ("name",), " Pixel"),
    ("window_status", ("name",), "x" * 65),
    ("window_status", ("name",), "a\tb"),
    ("window_status", ("lanes",), ["main", "main"]),
    ("window_status", ("lanes",), ["Main"]),
    ("window_status", ("lanes",), ["1st"]),
    ("window_status", ("lanes",), ["a" * 33]),
    ("window_status", ("lanes",), [f"l{i}" for i in range(33)]),
    ("window_status", ("lanes",), [1]),
    ("window_status", ("lanes",), [{"main": 1}]),
    ("window_status", ("attempts",), -1),
    ("rotate_status", ("devices_on_previous", 0, "name"), ""),
    ("rotate_status", ("state",), "none "),
]


@pytest.mark.parametrize(("op", "path", "bad"), FORMAT_CASES)
def test_a_result_value_outside_its_format_is_refused(op: str, path: tuple[Any, ...], bad: Any) -> None:
    expect(AnswerSchemaError, op, answer(changed(op, path, bad)))


def test_the_largest_lane_list_and_a_leap_day_are_accepted() -> None:
    result = changed("window_status", ("lanes",), [f"l{i}" for i in range(32)])
    result["expires_at"] = "2028-02-29T23:59:59Z"
    assert decode_answer(request("window_status"), answer(result)) == result


def test_a_revoke_answer_for_another_device_is_refused() -> None:
    expect(AnswerSchemaError, "revoke", answer({**VALID["revoke"], "device_id": "ffffffffffffffff"}))
