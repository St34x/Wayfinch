# SPDX-License-Identifier: Apache-2.0
"""``glasses.admin_protocol``: every accept and refuse case of the admin-socket wire format v1, on bytes."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable

import pytest

from glasses import admin_protocol as ap

DEV = "0123456789abcdef"


def line(body: object) -> bytes:
    return (json.dumps(body) + "\n").encode()


def ok_body(**over: object) -> dict[str, object]:
    return {"v": 1, "ok": True, "deviceId": DEV, "counts": {"sessions_closed": 1}, **over}


def test_the_request_is_one_sorted_compact_ascii_line():
    assert ap.revoke_request(DEV) == b'{"deviceId":"0123456789abcdef","op":"revoke","v":1}\n'
    assert len(ap.revoke_request(DEV)) <= ap.REQUEST_CAP


class _Sneaky(str):
    pass


@pytest.mark.parametrize(
    "value", ["", "../x", ";", "0" * 1000, DEV.upper(), DEV[:15], DEV + "0", "\x1b[2J", 12, None, _Sneaky(DEV)]
)
def test_a_request_for_anything_but_an_exact_device_id_is_refused(value):
    with pytest.raises(ap.AdminRequestRefused) as caught:
        ap.revoke_request(value)
    assert str(caught.value) == ap.AdminRequestRefused.message and caught.value.__cause__ is None


def test_a_confirming_answer_returns_its_counts():
    counts = {f"c{i}": i for i in range(ap.MAX_COUNTS)} | {"c0": ap.MAX_COUNT}
    assert ap.decode_revoke_answer(DEV, line(ok_body(counts=counts))) == counts
    assert ap.decode_revoke_answer(DEV, line(ok_body())) == {"sessions_closed": 1}


@pytest.mark.parametrize("code", sorted(ap.ERROR_CODES))
def test_a_well_formed_error_answer_is_a_refusal_with_its_code(code):
    with pytest.raises(ap.AdminRefusal) as caught:
        ap.decode_revoke_answer(DEV, line({"v": 1, "ok": False, "error": code}))
    assert caught.value.code == code


DEEP = b'{"v":1,"ok":true,"deviceId":"' + DEV.encode() + b'","counts":' + b"[" * 30_000 + b"]" * 30_000 + b"}\n"
HEAD = line(ok_body())[:-2]  # without "}\n"


def padded(size: int) -> bytes:
    """An otherwise valid confirming answer padded with JSON spaces before its closing brace to *size* bytes."""
    return HEAD + b" " * (size - len(HEAD) - 2) + b"}\n"


REJECTED = {
    "empty": b"",
    "no-newline": line(ok_body())[:-1],
    "byte-after-newline": line(ok_body()) + b" ",
    "blank-line-after-newline": line(ok_body()) + b"\n",
    "two-lines": line(ok_body()) * 2,
    "newline-inside-the-object": line(ok_body()).replace(b", ", b",\n", 1),
    "over-cap": b'{"v":1,"pad":"' + b"x" * ap.ANSWER_CAP + b'"}\n',
    "valid-but-over-cap": padded(ap.ANSWER_CAP + 1),
    "leading-space": b" " + line(ok_body()),
    "space-before-newline": line(ok_body())[:-1] + b" \n",
    "carriage-return": line(ok_body())[:-1] + b"\r\n",
    "not-json": b"hello\n",
    "not-an-object": b"[1]\n",
    "non-utf8": b'{"v":1,"ok":true,"deviceId":"\xff"}\n',
    "duplicate-key": b'{"v":1,"v":1,"ok":true,"deviceId":"' + DEV.encode() + b'","counts":{"a":1}}\n',
    "nan": line(ok_body(counts={"a": float("nan")})),
    "infinity": b'{"v":1,"ok":true,"deviceId":"' + DEV.encode() + b'","counts":{"a":Infinity}}\n',
    "deep-nesting": DEEP,
    "v-true": line(ok_body(v=True)),
    "v-float": b'{"v":1.0,"ok":true,"deviceId":"' + DEV.encode() + b'","counts":{"a":1}}\n',
    "v-2": line(ok_body(v=2)),
    "v-missing": line({k: v for k, v in ok_body().items() if k != "v"}),
    "ok-1": line(ok_body(ok=1)),
    "ok-missing": line({k: v for k, v in ok_body().items() if k != "ok"}),
    "other-device": line(ok_body(deviceId="fedcba9876543210")),
    "device-not-str": line(ok_body(deviceId=1)),
    "extra-key": line(ok_body(extra=1)),
    "counts-missing": line({k: v for k, v in ok_body().items() if k != "counts"}),
    "counts-empty": line(ok_body(counts={})),
    "counts-list": line(ok_body(counts=[1])),
    "17-counts": line(ok_body(counts={f"c{i}": 1 for i in range(17)})),
    "bool-count": line(ok_body(counts={"a": True})),
    "float-count": line(ok_body(counts={"a": 1.5})),
    "negative-count": line(ok_body(counts={"a": -1})),
    "huge-count": line(ok_body(counts={"a": ap.MAX_COUNT + 1})),
    "bad-count-name": line(ok_body(counts={"A": 1})),
    "long-count-name": line(ok_body(counts={"a" * 33: 1})),
    "escape-count-name": line(ok_body(counts={"a\x1b[2J": 1})),
    "error-unknown-code": line({"v": 1, "ok": False, "error": "marker-text"}),
    "error-code-list": line({"v": 1, "ok": False, "error": ["internal"]}),
    "error-extra-key": line({"v": 1, "ok": False, "error": "internal", "x": 1}),
    "error-ok-true": line({"v": 1, "ok": True, "error": "internal"}),
    "error-ok-0": line({"v": 1, "ok": 0, "error": "internal"}),
    "not-bytes": "text\n",
}


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_a_malformed_answer_is_rejected_with_a_fixed_message(case):
    with pytest.raises(ap.AdminAnswerRejected) as caught:
        ap.decode_revoke_answer(DEV, REJECTED[case])  # type: ignore[arg-type]
    assert str(caught.value) == "the plugin's answer was not valid"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is False


def test_the_deep_nesting_case_fits_the_cap_so_the_parser_sees_it():
    assert len(DEEP) <= ap.ANSWER_CAP  # else the size check rejects it before json.loads ever sees it


def _in_small_stack_thread(work: Callable[[], str]) -> list[str]:
    outcome: list[str] = []
    old = threading.stack_size(512 * 1024)
    try:
        worker = threading.Thread(target=lambda: outcome.append(work()))
        worker.start()
    finally:
        threading.stack_size(old)
    worker.join(10)
    return outcome


def _raw_parse() -> str:
    try:
        json.loads(DEEP[:-1])
    except RecursionError:
        return "recursion"
    return "parsed"


def _decode() -> str:
    try:
        ap.decode_revoke_answer(DEV, DEEP)
    except ap.AdminAnswerRejected:
        return "rejected"
    except RecursionError:
        return "recursion"
    return "accepted"


def test_a_recursion_error_in_the_parser_is_a_rejection():
    """The JSON scanner's depth limit is the C stack (3.14) or the recursion limit (3.11); on a 512 KiB thread
    stack ``DEEP`` overflows it for real, and the decoder turns that into ``AdminAnswerRejected``."""
    assert _in_small_stack_thread(_raw_parse) == ["recursion"]  # precondition: the parser really overflows
    assert _in_small_stack_thread(_decode) == ["rejected"]


def test_an_otherwise_valid_answer_of_exactly_the_cap_is_accepted():
    assert len(padded(ap.ANSWER_CAP)) == ap.ANSWER_CAP
    assert ap.decode_revoke_answer(DEV, padded(ap.ANSWER_CAP)) == {"sessions_closed": 1}
    assert len(padded(ap.ANSWER_CAP + 1)) == ap.ANSWER_CAP + 1


class _Liar(str):
    """A ``str`` subclass whose comparisons always say "equal": the decoder must not trust a caller's type."""

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    __hash__ = str.__hash__


@pytest.mark.parametrize("device_id", [_Liar(DEV), DEV.upper(), "", 12, None])
def test_the_decoder_rejects_a_device_id_that_is_not_an_exact_id(device_id):
    data = line(ok_body(deviceId="fedcba9876543210"))
    with pytest.raises(ap.AdminAnswerRejected):
        ap.decode_revoke_answer(device_id, data)  # type: ignore[arg-type]


def test_constants_match_the_wire_format():
    assert ap.SOCKET_NAME == "admin.sock" and (ap.REQUEST_CAP, ap.ANSWER_CAP) == (4096, 65_536)
    assert (ap.CONNECT_TIMEOUT_S, ap.CALL_TIMEOUT_S) == (2.0, 10.0)
    assert sorted(ap.OPS) == ["revoke", "status"] and ap.MAX_COUNTS == 16
