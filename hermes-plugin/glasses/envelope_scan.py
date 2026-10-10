# SPDX-License-Identifier: Apache-2.0
"""Pre-parse checks of an envelope frame: the frame caps and a scan of the raw text (pure, no I/O).

Runs before any JSON parser sees the payload (protocol/README.md, *Limits and validation notes*):

* **Frame caps** count the UTF-8 bytes of the WebSocket payload: ``FRAME_CAP`` for every frame, ``AUDIO_FRAME_CAP``
  for the types in ``AUDIO_TYPES``. ``scan`` reports the values of the top-level ``"type"`` keys it can read
  verbatim, so an audio frame over its cap is refused unparsed. A ``type`` key written with escapes is not read
  here; ``glasses.envelope`` checks the cap of the parsed type again, so the escape gains nothing.
* **Nesting depth**: Python's JSON decoder recurses once per nested array/object, so 64 KiB of ``[`` would end in a
  ``RecursionError``. ``scan`` counts the open brackets outside strings and stops at the first one deeper than
  ``MAX_DEPTH``: 35 levels pass (envelope = 1, body = 2, plus the 32 levels an open object may hold under an
  array: ``skill.manifest.skills[]``), 36 are refused. The per-field depth rules of the open objects run after the
  schema (``glasses.envelope_rules``).

The scan tokenises with the JSON string grammar, so a bracket inside a string never counts. On text that is not
JSON it can only count more brackets than the parser would reach (the parser stops at the first error), never fewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FRAME_CAP = 65_536
AUDIO_FRAME_CAP = 32_768
AUDIO_TYPES = frozenset({"audio.chunk", "meeting.audio"})
MAX_DEPTH = 35

# A JSON string (escapes included; an unterminated one runs to the end of the text, so the scan stays linear and the
# parser, which stops at that string, never reaches what follows) or one structural character; the rest is skipped.
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"?|[\[\]{}:,]', re.DOTALL)
_OPEN = frozenset("[{")
_CLOSE = frozenset("]}")
_TYPE_KEY = ('"type"', ":")


@dataclass(frozen=True)
class Scan:
    """What the raw text shows before parsing: too deep, and the verbatim top-level ``type`` values."""

    too_deep: bool
    top_level_types: frozenset[str]


def utf8_size(raw: bytes | str) -> int:
    """The payload size in UTF-8 bytes (a lone surrogate in ``str`` counts 3 bytes; it is refused after parsing)."""
    return len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8", "surrogatepass"))


def cap_for(frame_types: frozenset[str]) -> int:
    """The frame cap that applies when the frame's type is one of ``frame_types`` (the smaller cap wins)."""
    return AUDIO_FRAME_CAP if frame_types & AUDIO_TYPES else FRAME_CAP


def scan(text: str) -> Scan:
    """Count the nesting depth of ``text`` (stops early once too deep) and read its top-level ``type`` values."""
    depth = 0
    previous: tuple[str, str] = ("", "")
    types: set[str] = set()
    for match in _TOKEN.finditer(text):
        token = match.group()
        if token in _OPEN:
            depth += 1
            if depth > MAX_DEPTH:
                return Scan(too_deep=True, top_level_types=frozenset(types))
        elif token in _CLOSE:
            depth = max(depth - 1, 0)
        elif depth == 1 and previous == _TYPE_KEY and token.startswith('"'):
            types.add(token[1:-1])
        is_top_level_token = depth == 1 and token not in _OPEN and token not in _CLOSE
        previous = (previous[1], token) if is_top_level_token else ("", "")
    return Scan(too_deep=False, top_level_types=frozenset(types))
