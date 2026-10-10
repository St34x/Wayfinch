# SPDX-License-Identifier: Apache-2.0
"""Envelope codec, the sending side: ``encode`` emits only frames the server may send (pure, no I/O).

``encode(frame, schema)`` returns the JSON text of one WebSocket text message, or raises ``EncodeRefused`` to the
producer and returns nothing. The steps, in order:

1. serialise: ``json.dumps(..., allow_nan=False)`` (NaN and infinities are not JSON) into UTF-8 (a lone surrogate
   cannot be encoded); a text over ``FRAME_CAP`` is refused here, before any parsing or validation;
2. parse back with duplicate keys refused (``{1: .., "1": ..}`` would serialise to one key twice), so every later
   check sees exactly what the peer will read;
3. validate: the envelope schema (``FrameSchema``, injected as for ``decode``); the direction (a type only the device
   sends, ``D2S_ONLY``, is refused); the rules a schema cannot express for a frame the server sends
   (``glasses.envelope_rules``, ``from_device=False``): ``seq`` by direction, per-field depth, UTF-8 byte limits,
   Feb 29 of a common year, integer literals in integer fields;
4. measure: the UTF-8 size of the text against the frame cap of its type (``glasses.envelope_scan``).

So a returned text passes the injected schema, is an S→D or both-ways type, breaks none of those rules and fits its
frame cap. A refusal names only the failed check (``EncodeRefused.reason``), never frame content, and chains no
exception: producers log it without leaking message text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from .envelope import FrameSchema, unique_keys
from .envelope_rules import D2S_ONLY, violation
from .envelope_scan import FRAME_CAP, cap_for
from .generated.protocol_types import Frame


class EncodeRefused(ValueError):
    """``encode`` refused to emit a frame; ``reason`` names the failed check and never holds frame content.

    Reasons: ``not_json``, ``invalid_text``, ``frame_too_large`` (over ``FRAME_CAP`` before parsing, or over the cap
    of its type after validation), ``schema``, ``direction`` (a type only the device sends: the server never sends
    it), a ``glasses.envelope_rules.Violation`` value (``seq``, ``too_deep``, ``too_many_bytes``, ``feb_29``,
    ``not_integer``).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"frame not encoded: {reason}")
        self.reason = reason


def encode(frame: Frame, schema: FrameSchema) -> str:
    """The JSON text of ``frame``; raises ``EncodeRefused`` instead of emitting a frame that breaks the protocol."""
    text = _serialise(frame)
    size = _utf8_size(text)
    if size > FRAME_CAP:
        raise EncodeRefused("frame_too_large")
    value = _parse_back(text)
    if not schema.is_valid(value):
        raise EncodeRefused("schema")
    checked = cast(Mapping[str, object], value)  # schema-valid: an object with a string "type"
    frame_type = str(checked["type"])
    if frame_type in D2S_ONLY:
        raise EncodeRefused("direction")
    broken = violation(checked, from_device=False)
    if broken is not None:
        raise EncodeRefused(broken.value)
    if size > cap_for(frozenset({frame_type})):  # unreachable today: the smaller caps (AUDIO_TYPES) are D2S_ONLY
        raise EncodeRefused("frame_too_large")
    return text


def _serialise(frame: object) -> str:
    try:
        return json.dumps(frame, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):  # not serialisable, NaN/infinity, nested too deep
        pass
    raise EncodeRefused("not_json")  # outside the handler: the serialiser's message may quote a value


def _utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate
        pass
    raise EncodeRefused("invalid_text")  # outside the handler: the encode error holds the text


def _parse_back(text: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):  # a duplicate key; deep nesting the serialiser still managed
        pass
    raise EncodeRefused("not_json")
