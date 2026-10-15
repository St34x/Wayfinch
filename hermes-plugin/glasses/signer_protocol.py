# SPDX-License-Identifier: Apache-2.0
"""Wire protocol of the device-CA signer, client side (pure, no I/O; the socket client is ``signer_client``).

* **Requests** exist only for the seven ops this plugin sends (``OPS``; ``renew``: T1.185): a root-only or unknown op
  raises ``UnknownOp``. ``Request`` checks op, rid and fields itself (exact ``str`` types, each field in its format)
  and derives ``line``, so no request outside ``OPS`` can be built; the caller passes the ``rid`` (no CSPRNG here).
* **Answers** must be one line of at most ``ANSWER_CAP`` bytes ending in its only newline: one UTF-8 JSON object
  (item 14), the request's ``rid``, a boolean ``ok``, then exactly the result keys of the op (``RESULTS``) or exactly
  ``error`` plus the one extra its code allows. Anything else raises an ``AnswerRejected`` kind (fail closed); a
  well-formed error answer raises ``SignerRefusal``.

Formats chosen where the brief is silent (the plugin refuses anything else; the signer must emit exactly these):

1. Framing: the answer object is followed only by ``\\n``; no whitespace before or after it (no CR, blank or tab).
   The signer closes the connection right after that newline: the client reads to EOF, so a byte after it is
   ``AnswerNotJson`` and a connection held open ends in ``SignerTimeout`` (10 s; ``enrol`` 340 s).
2. Request ``rid``: exactly 32 lower-case hex (the brief allows 16..32).
3. Times: exactly ``YYYY-MM-DDTHH:MM:SSZ`` and a real calendar time (no fraction, offset, lower case or ``:60``).
4. Counts (``number``, ``crl_number``, ``attempts``, ``devices_on_previous``, ``retry_after_s``): a JSON integer
   0..2^63-1 (no float, no ``true``).
5. ``token``: canonical unpadded base64url, 43 chars. Requests: ``code`` 22 chars base64url with zero pad bits;
   ``spki``/``sig``: canonical padded base64 of 1..256 / 1..80 bytes.
6. ``cert``: exactly one ``CERTIFICATE`` PEM block, LF line ends only, body lines exactly 64 chars except the last
   (1..64), canonical base64 body; the final newline is optional. Its content is not parsed here.
7. ``subject_cn``, ``signer_version``: 1..256 chars, all printable (Unicode ``str.isprintable``, not only ASCII).
8. ``name`` (``window_status``, ``rotate_status`` entries): 1..64 printable chars, no surrounding whitespace; lanes
   ``[a-z][a-z0-9_-]{0,31}``, no duplicates, at most 32 (the ``window_open`` rules).
9. Window (``status.window``, ``window_status``): ``open`` exactly when ``expires_at`` is not null; a pending request
   only while open; an open ``window_status`` has a ``name`` and 1..32 lanes. A closed one may have ``name`` null,
   ``lanes`` ``[]`` and ``pending`` null. Nullable values are always present (``null``), never left out.
10. Rotation ``state``: ``none`` or ``overlap`` (``status`` and ``rotate_status``).
11. ``crl_generate`` success: at least one ``crl`` entry. ``status`` ``ca``, ``crl`` and ``edge.installed`` may be
    ``[]`` (CA not initialised yet).
12. Errors: any of the 14 codes on any op (each is a refusal). ``retry_after_s`` only with ``rate_limited``;
    ``registry_revoked`` only with ``crl_failed`` on ``revoke`` and only ``true``; both optional.
13. ``revoke`` echoes the requested ``device_id``; every object has exactly its keys at every level.
14. Answers (the brief says this only of requests): no duplicate keys, no ``NaN``/``Infinity``, ``v`` the JSON
    integer 1 (not ``1.0``/``true``); ``ca_id`` exactly 16 lower-case hex.

Error tree: ``SignerError`` → ``RequestRefused`` (``UnknownOp``, ``InvalidField``, ``RequestTooLarge``) /
``AnswerRejected`` (``AnswerTooLarge``, ``AnswerNotJson``, ``RidMismatch``, ``AnswerSchemaError``) / ``SignerRefusal``.
Messages are fixed per kind and never chain the parser error, so no input reaches a log line.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import ClassVar, TypeAlias

REQUEST_CAP = 16_384  # bytes, newline included
ANSWER_CAP = 65_536  # bytes, newline included
CONNECT_TIMEOUT_S = 2.0
OP_TIMEOUT_S = 10.0
ENROL_TIMEOUT_S = 340.0  # above the signer's 330 s hold while the operator confirms the enrolment
OPS = frozenset({"status", "enrol", "revoke", "crl_generate", "window_status", "window_close", "rotate_status"})
ERROR_CODES = frozenset({
    *("bad_request", "forbidden", "enrol_refused", "bad_proof", "known_key", "unknown_device", "crl_failed"),
    *("not_initialised", "exists", "rotation_state", "no_pending", "mismatch", "rate_limited", "internal"),
})
# Files the signer publishes in its public directory (contract names, not settings).
PUBLIC_CA_BUNDLE = "device-ca.pem"
PUBLIC_CRL = "devices-crl.pem"
PUBLIC_REGISTRY = "devices.json"

_ENVELOPE = frozenset({"v", "rid", "ok"})


class SignerError(Exception):
    """Base of every failed signer call. ``message`` is fixed per kind and never contains input."""

    message: ClassVar[str] = "signer call failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class RequestRefused(SignerError):
    message = "signer request refused before sending"


class UnknownOp(RequestRefused):
    message = "op is not one the plugin may send"


class InvalidField(RequestRefused):
    message = "request field missing, extra or malformed"


class RequestTooLarge(RequestRefused):
    message = "request line exceeds its size cap"


class AnswerRejected(SignerError):
    message = "signer answer rejected"


class AnswerTooLarge(AnswerRejected):
    message = "answer line exceeds its size cap"


class AnswerNotJson(AnswerRejected):
    message = "answer is not one line holding one strict JSON object"


class RidMismatch(AnswerRejected):
    message = "answer rid does not echo the request rid"


class AnswerSchemaError(AnswerRejected):
    message = "answer does not match the schema of its op"


class SignerRefusal(SignerError):
    """A well-formed error answer: ``code`` is one of ``ERROR_CODES``; each extra is set only with its code."""

    message = "signer refused the request"

    def __init__(self, code: str, retry_after_s: int | None, registry_revoked: bool | None) -> None:
        super().__init__()
        self.code, self.retry_after_s, self.registry_revoked = code, retry_after_s, registry_revoked


@dataclass(frozen=True)
class _ListOf:
    item: Spec


@dataclass(frozen=True)
class _Nullable:
    inner: Spec


@dataclass(frozen=True)
class _Where:
    inner: Spec
    holds: Check  # a rule across the values, checked once ``inner`` matched


Check: TypeAlias = Callable[[object], bool]
# A JSON object with exactly these keys | a list | null or the inner spec | inner plus a rule | one of these strings |
# a predicate.
Spec: TypeAlias = "Mapping[str, Spec] | _ListOf | _Nullable | _Where | frozenset[str] | Check"


def _obj(keys: Mapping[str, Spec]) -> Mapping[str, Spec]:
    return MappingProxyType(dict(keys))


def _pattern(regex: str) -> Check:
    compiled = re.compile(regex, re.ASCII)
    return lambda value: isinstance(value, str) and compiled.fullmatch(value) is not None


_hex16, _hex32, _hex64 = (_pattern(f"[0-9a-f]{{{n}}}") for n in (16, 32, 64))
_lane = _pattern(r"[a-z][a-z0-9_-]{0,31}")
_B64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?", re.ASCII)
_PEM = re.compile(
    r"-----BEGIN CERTIFICATE-----\n((?:[A-Za-z0-9+/]{64}\n)*[A-Za-z0-9+/=]{1,64}\n)-----END CERTIFICATE-----\n?",
    re.ASCII,
)
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)
_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"
_FROM_URL = str.maketrans("-_", "+/")


def _b64(max_bytes: int) -> Check:
    """Strict, canonical standard base64 (padding, zero pad bits, no whitespace) of 1..``max_bytes`` bytes."""

    def check(value: object) -> bool:
        if not isinstance(value, str) or _B64.fullmatch(value) is None:
            return False
        raw = base64.b64decode(value)
        return 0 < len(raw) <= max_bytes and base64.b64encode(raw).decode("ascii") == value

    return check


def _b64url(n_bytes: int) -> Check:
    """Canonical unpadded base64url of exactly ``n_bytes`` bytes."""
    shape, standard = _pattern(f"[A-Za-z0-9_-]{{{(n_bytes * 8 + 5) // 6}}}"), _b64(n_bytes)
    return lambda value: shape(value) and standard(f"{value}{'=' * (-len(str(value)) % 4)}".translate(_FROM_URL))


def _is_time(value: object) -> bool:
    """RFC 3339 UTC with seconds, ``2026-10-11T12:00:00Z``, and a real calendar time."""
    try:
        return (
            isinstance(value, str) and _TIME.fullmatch(value) is not None and bool(datetime.strptime(value, _TIME_FMT))
        )
    except ValueError:
        return False


def _is_cert_pem(value: object) -> bool:
    match = _PEM.fullmatch(value) if isinstance(value, str) else None
    return match is not None and _b64(ANSWER_CAP)(match.group(1).replace("\n", ""))


def _text(max_chars: int) -> Check:
    return lambda value: isinstance(value, str) and 0 < len(value) <= max_chars and value.isprintable()


def _is_name(value: object) -> bool:
    return _text(64)(value) and value == str(value).strip()


def _is_lanes(value: object) -> bool:
    lanes = value if isinstance(value, list) and all(_lane(lane) for lane in value) else None
    return lanes is not None and len(set(lanes)) == len(lanes) <= 32


def _is_count(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**63


def _is_bool(value: object) -> bool:
    return type(value) is bool


def _coherent_window(value: object) -> bool:
    """Open exactly with an expiry, pending only while open; an open ``window_status`` names its phone and lanes."""
    window = value if isinstance(value, dict) else {}
    is_open = window.get("open") is True
    if is_open != (window.get("expires_at") is not None) or (window.get("pending") and not is_open):
        return False
    return not is_open or "name" not in window or (window["name"] is not None and len(window["lanes"]) > 0)


_TEXT, _TIME_OR_NULL, _STATES = _text(256), _Nullable(_is_time), frozenset({"none", "overlap"})
_CA = _obj({"ca_id": _hex16, "subject_cn": _TEXT, "not_after": _is_time, "role": frozenset({"current", "previous"})})
_CRL = _obj({"ca_id": _hex16, "number": _is_count, "this_update": _is_time, "next_update": _is_time, "sha256": _hex64})
_CRL_SHORT = _obj({"ca_id": _hex16, "number": _is_count, "next_update": _is_time})
_EDGE_STATES = frozenset({"ok", "refused", "reload_failed", "not_configured", "unknown"})
_EDGE = _obj({"state": _EDGE_STATES, "installed": _ListOf(_CRL_SHORT), "last_attempt_at": _TIME_OR_NULL})
_WINDOW = _obj({"open": _is_bool, "expires_at": _TIME_OR_NULL, "pending": _is_bool})
_WINDOW_STATUS = _obj({
    "open": _is_bool,
    "expires_at": _TIME_OR_NULL,
    "name": _Nullable(_is_name),
    "lanes": _is_lanes,
    "attempts": _is_count,
    "pending": _Nullable(_obj({"spki_sha256": _hex64, "since": _is_time})),
})
_ROTATED = _obj({"device_id": _hex16, "name": _is_name})
RESULTS: Mapping[str, Spec] = MappingProxyType({
    "status": _obj({
        "ca": _ListOf(_CA),
        "crl": _ListOf(_CRL),
        "edge": _EDGE,
        "window": _Where(_WINDOW, _coherent_window),
        "rotation": _obj({"state": _STATES, "devices_on_previous": _is_count}),
        "signer_version": _TEXT,
    }),
    "enrol": _obj({"device_id": _hex16, "cert": _is_cert_pem, "token": _b64url(32)}),
    "revoke": _obj({"device_id": _hex16, "already_revoked": _is_bool, "crl_number": _is_count}),
    "crl_generate": _obj({"crl": _Where(_ListOf(_CRL_SHORT), bool)}),
    "window_status": _Where(_WINDOW_STATUS, _coherent_window),
    "window_close": _obj({"was_open": _is_bool}),
    "rotate_status": _obj({"state": _STATES, "devices_on_previous": _ListOf(_ROTATED)}),
})
_NO_FIELDS: Mapping[str, Check] = MappingProxyType({})
_FIELDS: Mapping[str, Mapping[str, Check]] = MappingProxyType({
    **dict.fromkeys(OPS, _NO_FIELDS),
    "enrol": MappingProxyType({"spki": _b64(256), "code": _b64url(16), "sig": _b64(80)}),
    "revoke": MappingProxyType({"device_id": _hex16}),
})
# The one extra an error code may carry, the ops it may come with, and its check.
_ERROR_EXTRAS: Mapping[str, tuple[str, frozenset[str], Check]] = MappingProxyType({
    "rate_limited": ("retry_after_s", OPS, _is_count),
    "crl_failed": ("registry_revoked", frozenset({"revoke"}), lambda value: value is True),
})


def _valid(value: object, spec: Spec) -> bool:
    if isinstance(spec, Mapping):
        if not isinstance(value, dict) or value.keys() != spec.keys():
            return False
        return all(_valid(value[key], inner) for key, inner in spec.items())
    if isinstance(spec, _ListOf):
        return isinstance(value, list) and all(_valid(item, spec.item) for item in value)
    if isinstance(spec, _Nullable):
        return value is None or _valid(value, spec.inner)
    if isinstance(spec, _Where):
        return _valid(value, spec.inner) and spec.holds(value)
    if isinstance(spec, frozenset):
        return isinstance(value, str) and value in spec
    return spec(value)


@dataclass(frozen=True)
class Request:
    """A request ready to send, checked on construction: ``op`` in ``OPS`` (else ``UnknownOp``), ``rid`` 32 lower-case
    hex and exactly the op's fields in their formats (else ``InvalidField``), all exact ``str``. ``line`` is derived:
    the exact bytes, newline included. Field values stay out of ``repr``."""

    op: str
    rid: str
    fields: Mapping[str, str] = field(repr=False)
    line: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        given = _checked_fields(self.op, self.rid, self.fields)
        object.__setattr__(self, "fields", MappingProxyType(given))
        object.__setattr__(self, "line", _encode_line({"v": 1, "op": self.op, "rid": self.rid, **given}))


def _checked_fields(op: object, rid: object, fields: object) -> dict[str, str]:
    if type(op) is not str or op not in OPS:
        raise UnknownOp
    if type(rid) is not str or not _hex32(rid) or not isinstance(fields, Mapping):
        raise InvalidField
    given, formats = dict(fields), _FIELDS[op]
    exact = all(type(key) is str and type(value) is str for key, value in given.items())
    if not exact or given.keys() != formats.keys() or not all(ok(given[key]) for key, ok in formats.items()):
        raise InvalidField
    return given


def _encode_line(message: Mapping[str, str | int]) -> bytes:
    """Compact ASCII JSON of ``message`` plus the newline; over ``REQUEST_CAP`` bytes raises ``RequestTooLarge``."""
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    if len(line) > REQUEST_CAP:
        raise RequestTooLarge
    return line


def build_request(op: str, rid: str, fields: Mapping[str, str] | None = None) -> Request:
    """The request for ``op`` with exactly the fields of its op, each in its format; else a ``RequestRefused`` kind."""
    return Request(op, rid, {} if fields is None else fields)


def decode_answer(request: Request, line: bytes) -> dict[str, object]:
    """The result fields of a success answer to ``request``; an error answer raises ``SignerRefusal``."""
    message = _parse_line(line)
    _check_envelope(message, request.rid)
    if message["ok"] is False:
        raise _refusal(message, request.op)
    result = {key: value for key, value in message.items() if key not in _ENVELOPE}
    if not _valid(result, RESULTS[request.op]):
        raise AnswerSchemaError
    if request.op == "revoke" and result["device_id"] != request.fields["device_id"]:
        raise AnswerSchemaError
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")  # caught in _parse_line
    return obj


def _refuse_constant(_name: str) -> object:
    raise ValueError("non-finite number")  # caught in _parse_line


def _parse_line(line: bytes) -> dict[str, object]:
    if len(line) > ANSWER_CAP:
        raise AnswerTooLarge
    if not line.endswith(b"\n") or b"\n" in line[:-1]:
        raise AnswerNotJson
    try:
        text = line[:-1].decode("utf-8")
        bare = text[:1] == "{" and text[-1:] == "}"  # no whitespace around the object
        message = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_refuse_constant) if bare else None
    except (ValueError, RecursionError):
        message = None
    if not isinstance(message, dict):
        raise AnswerNotJson  # outside the handler: the parser error (it holds input) is not chained
    return message


def _check_envelope(message: dict[str, object], rid: str) -> None:
    v, answer_rid = message.get("v"), message.get("rid")
    if type(v) is not int or v != 1 or not isinstance(answer_rid, str) or not _is_bool(message.get("ok")):
        raise AnswerSchemaError
    if answer_rid != rid:
        raise RidMismatch


def _refusal(message: dict[str, object], op: str) -> SignerRefusal:
    code = message.get("error")
    if not isinstance(code, str) or code not in ERROR_CODES:
        raise AnswerSchemaError
    extras = {key: message[key] for key in message.keys() - _ENVELOPE - {"error"}}
    name, ops, check = _ERROR_EXTRAS.get(code, ("", frozenset(), _is_bool))
    if extras.keys() - ({name} if op in ops else set()) or not all(check(value) for value in extras.values()):
        raise AnswerSchemaError
    retry, revoked = extras.get("retry_after_s"), extras.get("registry_revoked")
    return SignerRefusal(
        code, retry if isinstance(retry, int) else None, revoked if isinstance(revoked, bool) else None
    )
