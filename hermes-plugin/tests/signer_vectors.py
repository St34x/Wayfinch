# SPDX-License-Identifier: Apache-2.0
"""Shared test vectors of the signer wire protocol: a valid result per op, request and answer line builders, the
typed-refusal assertion and helpers that walk a result to mutate one key or value."""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest

from glasses import signer_protocol as sp
from glasses.signer_protocol import SignerError, build_request, decode_answer

RID = "0123456789abcdef0123456789abcdef"
DEVICE = "00112233aabbccdd"
TIME = "2026-10-11T12:00:00Z"
H64 = "ab" * 32
ROOT_ONLY_OPS = ("window_open", "window_confirm", "ca_init", "rotate_begin", "rotate_finish", "rotate_compromise")


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


BODY = b64(bytes(range(200)))
PEM = "-----BEGIN CERTIFICATE-----\n" + "".join(f"{BODY[i : i + 64]}\n" for i in range(0, len(BODY), 64))
PEM += "-----END CERTIFICATE-----\n"
ENROL_FIELDS = {"spki": b64(bytes(91)), "code": b64url(bytes(range(16))), "sig": b64(bytes(70))}
FIELDS: dict[str, dict[str, str]] = {"enrol": ENROL_FIELDS, "revoke": {"device_id": DEVICE}}
CRL = {"ca_id": DEVICE, "number": 7, "next_update": TIME}
VALID: dict[str, dict[str, Any]] = {
    "status": {
        "ca": [{"ca_id": DEVICE, "subject_cn": "Wayfinch device CA", "not_after": TIME, "role": "current"}],
        "crl": [{"ca_id": DEVICE, "number": 7, "this_update": TIME, "next_update": TIME, "sha256": H64}],
        "edge": {"state": "ok", "installed": [CRL], "last_attempt_at": TIME},
        "window": {"open": True, "expires_at": TIME, "pending": False},
        "rotation": {"state": "none", "devices_on_previous": 0},
        "signer_version": "1.1.0",
    },
    "enrol": {"device_id": DEVICE, "cert": PEM, "token": b64url(bytes(range(32)))},
    "revoke": {"device_id": DEVICE, "already_revoked": False, "crl_number": 8},
    "crl_generate": {"crl": [CRL]},
    "window_status": {
        "open": True,
        "expires_at": TIME,
        "name": "Pixel 9",
        "lanes": ["main", "work-2"],
        "attempts": 1,
        "pending": {"spki_sha256": H64, "since": TIME},
    },
    "window_close": {"was_open": True},
    "rotate_status": {"state": "overlap", "devices_on_previous": [{"device_id": DEVICE, "name": "Pixel 9"}]},
}
NULLS = {
    "status": {"edge": {"state": "unknown", "installed": [], "last_attempt_at": None}},
    "window_status": {"open": False, "expires_at": None, "name": None, "lanes": [], "pending": None},
}


def request(op: str) -> sp.Request:
    return build_request(op, RID, FIELDS.get(op))


def line(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode() + b"\n"


def answer(result: dict[str, Any], rid: str = RID) -> bytes:
    return line({"v": 1, "rid": rid, "ok": True, **result})


def error(code: str, **extra: Any) -> bytes:
    return line({"v": 1, "rid": RID, "ok": False, "error": code, **extra})


def expect(kind: type[SignerError], op: str, raw: bytes) -> SignerError:
    """Decoding ``raw`` raises exactly a ``kind`` with its fixed message and no chained parser error."""
    with pytest.raises(kind) as caught:
        decode_answer(request(op), raw)
    assert str(caught.value) == kind.message and caught.value.__context__ is None
    return caught.value


def object_paths(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Paths of every JSON object in ``value`` (``()`` is the result itself); lists contribute their first item."""
    if isinstance(value, dict):
        yield path
        for key, item in value.items():
            yield from object_paths(item, (*path, key))
    elif isinstance(value, list) and value:
        yield from object_paths(value[0], (*path, 0))


def value_paths(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Paths of every value below the result, containers included; lists contribute their first item."""
    items = value.items() if isinstance(value, dict) else enumerate(value[:1]) if isinstance(value, list) else []
    for key, item in items:
        yield (*path, key)
        yield from value_paths(item, (*path, key))


def at(value: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        value = value[key]
    return value


def changed(op: str, path: tuple[Any, ...], new: Any = None, *, drop: bool = False) -> dict[str, Any]:
    """A deep copy of the valid ``op`` result with the value at ``path`` replaced by ``new`` (or removed)."""
    result = copy.deepcopy(VALID[op])
    parent = at(result, path[:-1])
    if drop:
        del parent[path[-1]]
    else:
        parent[path[-1]] = new
    return result


def ident(cases: list[tuple[str, tuple[Any, ...]]]) -> list[str]:
    return [f"{op}:{'.'.join(map(str, path)) or 'result'}" for op, path in cases]
