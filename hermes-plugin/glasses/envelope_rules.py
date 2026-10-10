# SPDX-License-Identifier: Apache-2.0
"""Envelope rules a JSON Schema cannot express; checked on a schema-valid frame (pure, no I/O).

``violation(frame, from_device=...)`` names the first rule a frame breaks, or returns ``None``
(protocol/README.md, *Delivery* and *Limits and validation notes*):

* ``seq`` by direction: never on a D→S frame (``error`` included); required on the S→D-only types and forbidden on
  ``ping``/``pong`` and the D→S-only types when the server sends; optional only on an S→D ``error``. The schema
  cannot see the direction: it requires ``seq`` on the S→D-only types, so a D→S frame of such a type fails either here
  or at the schema.
* nesting depth of the open objects ``skill.call.args``, ``skill.result.output`` and each ``skill.manifest.skills[]``
  item: the named value is level 1, each nested object or array adds 1, scalars add none; above 32 is refused. This
  count is authoritative; the raw-text pre-scan of ``glasses.envelope_scan`` only bounds the parser's recursion.
* UTF-8 byte limits of the README limits table (``BYTE_LIMITS``); ``msg.out.text`` has 512 B when ``longRef`` is
  present. The schema's ``maxLength`` is the same number counted in code points, so only the bytes can still fail.
* Feb 29 of a common year in a timestamp field (``ts``, ``welcome.serverTime``, ``approval.request.expiresAt``): the
  schema's pattern allows Feb 1-29 in every year.
* integer fields hold JSON integer literals: JSON Schema counts ``1.0`` and ``1e0`` as integers, but the phone's codec
  reads these fields as ``Int`` and would refuse or truncate them. The schema types no field as ``number``, so in a
  schema-valid frame a float outside the open objects can only sit in an ``integer`` field: any such float is
  refused. Floats inside the open objects (``OPEN_FIELDS``) are data and stay allowed.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Iterator, Mapping
from typing import Final

from .generated.protocol_types import FRAME_TYPES


class Violation(enum.Enum):
    """The rule a frame breaks; ``glasses.envelope`` maps each to its error frame."""

    SEQ = "seq"
    TOO_DEEP = "too_deep"
    TOO_MANY_BYTES = "too_many_bytes"
    FEB_29 = "feb_29"
    NOT_INTEGER = "not_integer"


S2D_ONLY: Final = frozenset({
    "welcome",
    "msg.out",
    "long.status",
    "approval.request",
    "clarify.request",
    "card.state",
    "hud.state",
    "skill.call",
    "skill.cancel",
    "meeting.state",
    "meeting.hint",
})
BOTH_WAYS: Final = frozenset({"ping", "pong", "error"})
D2S_ONLY: Final = frozenset(FRAME_TYPES) - S2D_ONLY - BOTH_WAYS

MAX_FIELD_DEPTH: Final = 32
# (frame type, path inside ``body``) of the open objects; ``[]`` = every item of the list.
OPEN_FIELDS: Final = (("skill.call", "args"), ("skill.result", "output"), ("skill.manifest", "skills[]"))

# (frame type, path inside ``body``, max UTF-8 bytes): one entry per field of the README limits table.
BYTE_LIMITS: Final = (
    ("msg.in", "text", 2000),
    ("msg.out", "text", 2048),
    ("msg.out", "longRef.fullText", 4096),
    ("approval.request", "summary", 300),
    ("approval.request", "detail", 4000),
    ("clarify.request", "question", 2000),
    ("clarify.request", "choices[]", 200),
    ("clarify.response", "answer", 2000),
    ("meeting.start", "prepTerms[]", 100),
    ("meeting.hint", "text", 180),
    ("error", "message", 500),
    ("skill.result", "error", 500),
    ("hello", "appVersion", 64),
    ("hello", "glasses.model", 64),
    ("hello", "glasses.fw", 64),
    ("meeting.state", "provider", 64),
    ("audio.chunk", "chunk", 31744),
    ("meeting.audio", "chunk", 31744),
)
MSG_OUT_TEXT_WITH_LONGREF: Final = 512

# Timestamp fields: (frame type, path inside ``body``); the envelope's ``ts`` is checked on every frame.
TIMESTAMP_FIELDS: Final = (("welcome", "serverTime"), ("approval.request", "expiresAt"))


def violation(frame: Mapping[str, object], *, from_device: bool) -> Violation | None:
    """The first rule the schema-valid ``frame`` breaks, or ``None``; ``from_device`` = the frame travels D→S."""
    frame_type = str(frame["type"])
    body = frame["body"]
    if not _seq_allowed(frame, frame_type, from_device):
        return Violation.SEQ
    open_values = _values(body, (path for owner, path in OPEN_FIELDS if owner == frame_type))
    if _has_float_outside(frame, open_values):
        return Violation.NOT_INTEGER
    if any(_deeper_than(value, MAX_FIELD_DEPTH) for value in open_values):
        return Violation.TOO_DEEP
    if any(_utf8_len(text) > limit for text, limit in _limited_strings(frame_type, body)):
        return Violation.TOO_MANY_BYTES
    stamps = [frame["ts"], *_values(body, (path for owner, path in TIMESTAMP_FIELDS if owner == frame_type))]
    if any(isinstance(stamp, str) and _is_feb_29_of_common_year(stamp) for stamp in stamps):
        return Violation.FEB_29
    return None


def _seq_allowed(frame: Mapping[str, object], frame_type: str, from_device: bool) -> bool:
    has_seq = "seq" in frame
    if from_device:
        return not has_seq
    if frame_type in S2D_ONLY:
        return has_seq
    return frame_type == "error" or not has_seq


def _values(body: object, paths: Iterable[str]) -> list[object]:
    """Every value at the dotted ``paths`` inside ``body`` (absent optional fields yield nothing)."""
    found: list[object] = []
    for path in paths:
        values = [body]
        for part in path.split("."):
            key = part.removesuffix("[]")
            values = [value[key] for value in values if isinstance(value, Mapping) and key in value]
            if part.endswith("[]"):
                values = [item for value in values if isinstance(value, list) for item in value]
        found.extend(values)
    return found


def _limited_strings(frame_type: str, body: object) -> Iterator[tuple[str, int]]:
    """Every string of ``body`` under a byte limit, with its limit (a field may come twice: its strictest wins)."""
    rows = [(path, limit) for owner, path, limit in BYTE_LIMITS if owner == frame_type]
    if frame_type == "msg.out" and isinstance(body, Mapping) and "longRef" in body:
        rows.append(("text", MSG_OUT_TEXT_WITH_LONGREF))
    for path, limit in rows:
        yield from ((text, limit) for text in _values(body, [path]) if isinstance(text, str))


def _has_float_outside(frame: Mapping[str, object], open_values: list[object]) -> bool:
    """True when a float sits in ``frame`` outside the ``open_values`` (objects, skipped by identity)."""
    skipped = {id(value) for value in open_values}
    pending: list[object] = [frame]
    while pending:
        item = pending.pop()
        if isinstance(item, float):
            return True
        if isinstance(item, Mapping) and id(item) not in skipped:
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _deeper_than(value: object, limit: int) -> bool:
    """True when ``value`` nests objects/arrays more than ``limit`` levels (itself = 1). Iterative: no recursion."""
    pending = [(value, 1)]
    while pending:
        item, level = pending.pop()
        if isinstance(item, dict):
            children = list(item.values())
        elif isinstance(item, list):
            children = item
        else:
            continue
        if level > limit:
            return True
        pending.extend((child, level + 1) for child in children)
    return False


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))  # lone surrogates are refused before; measuring never raises


def _is_feb_29_of_common_year(stamp: str) -> bool:
    if stamp[4:10] != "-02-29":
        return False
    year = int(stamp[:4])  # four ASCII digits: the schema's pattern
    return not (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
