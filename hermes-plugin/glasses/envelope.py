# SPDX-License-Identifier: Apache-2.0
"""Envelope codec of the glasses protocol: ``decode`` and the standard error frames (pure, no I/O).

``decode(raw, schema)`` turns one WebSocket payload into a ``Frame`` (the generated ``TypedDict`` union of
``glasses.generated.protocol_types``; narrow it on ``frame["type"]``). The steps, in order, each refusing with its own
``FrameRejected`` subclass:

1. frame cap on the raw payload, before anything parses it (``glasses.envelope_scan``): 65 536 B, and 32 768 B for
   ``audio.chunk``/``meeting.audio`` when the raw text shows that type;
2. UTF-8 (bytes input) and the nesting pre-scan: more than 35 nested open brackets are refused unparsed;
3. JSON parse (``NaN``/``Infinity``, a number too large for a finite float such as ``1e999``, numbers Python refuses
   to convert and a key repeated in one object count as invalid JSON: parsers differ on which duplicate wins, so the
   phone and the server could read different frames);
4. the cap of the parsed ``type`` again (a ``type`` key spelled with escapes escapes step 1, not this one);
5. strings that are not Unicode scalar values (lone surrogates, e.g. from a ``\\uD800`` escape), keys included;
6. the envelope schema (``protocol/envelope.schema.json``, JSON Schema 2020-12);
7. the rules a schema cannot express (``glasses.envelope_rules``), for a frame from the device (D→S, the only
   direction the server decodes): ``seq`` direction, per-field depth, UTF-8 byte limits, Feb 29 of a common year,
   integer literals (not ``1.0``/``1e0``) in integer fields.

The schema arrives as a port (``FrameSchema``): this core module neither reads files nor imports ``jsonschema``.
The shell builds it once, e.g. ``jsonschema.Draft202012Validator(schema)``, which has the needed ``is_valid``.

Error frames never echo input: every ``FrameRejected`` carries a fixed ``code`` and ``message`` per kind, and is
raised outside the handler of the parser error, so no exception context holds the payload either.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import ClassVar, Protocol, cast

from .envelope_rules import Violation, violation
from .envelope_scan import FRAME_CAP, cap_for, scan, utf8_size
from .generated.protocol_types import ErrorFrame, Frame, Timestamp, Ulid

# A UTF-16 surrogate code point: never a Unicode scalar value on its own (json.loads joins valid escaped pairs).
_SURROGATE = re.compile("[\\ud800-\\udfff]")


class FrameSchema(Protocol):
    """The envelope schema as a validator: True when ``instance`` is a schema-valid frame."""

    def is_valid(self, instance: object) -> bool: ...


class FrameRejected(ValueError):
    """A payload ``decode`` refuses. ``code`` and ``message`` are fixed per kind: they never contain input."""

    code: ClassVar[str] = "invalid_frame"
    message: ClassVar[str] = "frame rejected"

    def __init__(self) -> None:
        super().__init__(self.message)


class FrameTooLarge(FrameRejected):
    code = "frame_too_large"
    message = "frame exceeds its size cap"


class FrameTooDeep(FrameRejected):
    code = "frame_too_deep"
    message = "frame nesting exceeds the depth limit"


class InvalidJson(FrameRejected):
    code = "invalid_json"
    message = "frame is not valid JSON"


class InvalidText(FrameRejected):
    code = "invalid_text"
    message = "frame text is not valid UTF-8 or holds a lone surrogate"


class InvalidFrame(FrameRejected):
    code = "invalid_frame"
    message = "frame does not match the envelope schema"


class InvalidSeq(FrameRejected):
    code = "invalid_seq"
    message = "frame seq is not allowed for its type and direction"


class FieldTooLong(FrameRejected):
    code = "field_too_long"
    message = "a frame field exceeds its UTF-8 byte limit"


_RULE_ERRORS: dict[Violation, type[FrameRejected]] = {
    Violation.SEQ: InvalidSeq,
    Violation.TOO_DEEP: FrameTooDeep,
    Violation.TOO_MANY_BYTES: FieldTooLong,
    Violation.FEB_29: InvalidFrame,  # the date the schema's pattern would refuse if a pattern could count leap years
    Violation.NOT_INTEGER: InvalidFrame,  # 1.0 / 1e0 in an integer field: the schema accepts it, the phone would not
}


def decode(raw: bytes | str, schema: FrameSchema) -> Frame:
    """The frame in ``raw`` (one WebSocket payload); raises a ``FrameRejected`` subclass (module docstring)."""
    if len(raw) > FRAME_CAP:  # every code point is at least one UTF-8 byte: refuse a huge str before encoding it
        raise FrameTooLarge
    size = utf8_size(raw)
    if size > FRAME_CAP:
        raise FrameTooLarge
    text = _text(raw)
    found = scan(text)
    if found.too_deep:
        raise FrameTooDeep
    if size > cap_for(found.top_level_types):
        raise FrameTooLarge
    value = _parse(text)
    if size > cap_for(_parsed_type(value)):
        raise FrameTooLarge
    if _has_lone_surrogate(value):
        raise InvalidText
    if not schema.is_valid(value):
        raise InvalidFrame
    broken = violation(cast(Mapping[str, object], value), from_device=True)
    if broken is not None:
        raise _RULE_ERRORS[broken]
    return cast(Frame, value)


def error_frame(rejected: FrameRejected, frame_id: Ulid, ts: Timestamp) -> ErrorFrame:
    """The standard ``error`` frame for ``rejected``; ``frame_id`` and ``ts`` come from the caller (no clock here).

    Connection-scoped, so without ``seq`` (protocol/README.md, Delivery) and without ``corr``: a rejected frame has
    no trusted id to answer.
    """
    return {
        "v": 1,
        "id": frame_id,
        "type": "error",
        "ts": ts,
        "body": {"code": rejected.code, "message": rejected.message},
    }


def _text(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    raise InvalidText  # outside the handler: the decode error (it holds the payload) is not chained


def _reject_constant(_name: str) -> object:
    raise ValueError("NaN and Infinity are not JSON")


def _finite_float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):  # an overflowing literal such as 1e999 would become inf
        raise ValueError("number out of range")
    return value


def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``object_pairs_hook`` for ``json.loads`` that refuses a key repeated in one object (``ValueError``)."""
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("duplicate object key")
    return found


def _parse(text: str) -> object:
    try:
        return json.loads(
            text, parse_constant=_reject_constant, parse_float=_finite_float, object_pairs_hook=unique_keys
        )
    except ValueError:  # JSONDecodeError, a refused constant or duplicate key, an integer over Python's digit limit
        pass
    raise InvalidJson  # outside the handler: JSONDecodeError.doc holds the payload, so it is not chained


def _parsed_type(value: object) -> frozenset[str]:
    frame_type = value.get("type") if isinstance(value, dict) else None
    return frozenset({frame_type}) if isinstance(frame_type, str) else frozenset()


def _has_lone_surrogate(value: object) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False
