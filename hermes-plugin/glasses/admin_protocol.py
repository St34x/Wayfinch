# SPDX-License-Identifier: Apache-2.0
"""Wire format v1 of the plugin's admin socket ``$state_dir/admin.sock`` (pure core: no I/O).

One request line per connection, ``{"deviceId":"<id>","op":"revoke","v":1}`` + newline (sorted keys, no spaces,
ASCII), and one answer line of at most ``ANSWER_CAP`` bytes, then the server closes. The answer line is exactly one
JSON object (first byte ``{``, last byte before the newline ``}``) followed by a single newline, its only one; no
leading or trailing whitespace, no ``\\r``. Success is exactly
``{"v":1,"ok":true,"deviceId":"<echo>","counts":{"<name>":<int>}}`` (1..16 counts), an error exactly
``{"v":1,"ok":false,"error":"<code>"}``. The client is ``glasses.admin_devices`` (T1.15e); the server half
(T1.63a, planned) answers exactly this. Errors carry a fixed ``message`` per kind, never input text.
"""

from __future__ import annotations

import json
import re
from typing import Any

SOCKET_NAME = "admin.sock"
REQUEST_CAP = 4096  # bytes, newline included
ANSWER_CAP = 65_536  # bytes, newline included
CONNECT_TIMEOUT_S = 2.0
CALL_TIMEOUT_S = 10.0  # one deadline from connect to EOF
OPS = frozenset({"revoke", "status"})
ERROR_CODES = frozenset({"bad_request", "unknown_op", "internal"})
MAX_COUNTS = 16
MAX_COUNT = 2**31 - 1
VERSION = 1

_DEVICE_ID = re.compile(r"[0-9a-f]{16}")
_COUNT_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_SUCCESS_KEYS = frozenset({"v", "ok", "deviceId", "counts"})
_ERROR_KEYS = frozenset({"v", "ok", "error"})


class AdminError(Exception):
    """Base of every admin-socket error; ``message`` is fixed per kind."""

    message = "admin socket call failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class AdminRequestRefused(AdminError):
    message = "not a device id (16 lower-case hex)"


class AdminAnswerRejected(AdminError):
    message = "the plugin's answer was not valid"


class AdminRefusal(AdminError):
    message = "the plugin refused"

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code


def is_device_id(value: object) -> bool:
    """True for an exact ``str`` fully matching ``[0-9a-f]{16}``."""
    return type(value) is str and _DEVICE_ID.fullmatch(value) is not None


def revoke_request(device_id: object) -> bytes:
    """The ``revoke`` request line for *device_id*, else ``AdminRequestRefused``."""
    if not is_device_id(device_id):
        raise AdminRequestRefused
    body = {"deviceId": device_id, "op": "revoke", "v": VERSION}
    return (json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")  # caught in _parse
    return obj


def _no_constant(_name: str) -> None:
    raise ValueError("NaN or Infinity")  # caught in _parse


def _parse(data: object) -> dict[str, Any] | None:
    """The one JSON object ``{...}`` of an answer line within ``ANSWER_CAP`` ending in its only newline, else None."""
    if type(data) is not bytes or len(data) > ANSWER_CAP or data.find(b"\n") != len(data) - 1:
        return None
    if data[:1] != b"{" or data[-2:] != b"}\n":
        return None
    try:
        text = data[:-1].decode("utf-8")
        message = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return None
    return message if isinstance(message, dict) else None


def _valid_counts(counts: object) -> bool:
    if not isinstance(counts, dict) or not 1 <= len(counts) <= MAX_COUNTS:
        return False
    return all(
        _COUNT_NAME.fullmatch(name) is not None and type(value) is int and 0 <= value <= MAX_COUNT
        for name, value in counts.items()
    )


def decode_revoke_answer(device_id: str, data: bytes) -> dict[str, int]:
    """The counts of a confirming answer to ``revoke_request(device_id)``; ``AdminRefusal`` for a well-formed
    error answer, ``AdminAnswerRejected`` for anything else (also for a *device_id* that is not an exact id)."""
    if not is_device_id(device_id):
        raise AdminAnswerRejected
    message = _parse(data)
    if message is None or type(message.get("v")) is not int or message["v"] != VERSION:
        raise AdminAnswerRejected
    ok = message.get("ok")
    code = message.get("error")
    if ok is False and set(message) == _ERROR_KEYS and type(code) is str and code in ERROR_CODES:
        raise AdminRefusal(code)
    if ok is not True or set(message) != _SUCCESS_KEYS:
        raise AdminAnswerRejected
    if type(message["deviceId"]) is not str or message["deviceId"] != device_id:
        raise AdminAnswerRejected
    if not _valid_counts(message["counts"]):
        raise AdminAnswerRejected
    return dict(message["counts"])
