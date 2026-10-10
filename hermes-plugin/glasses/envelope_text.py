# SPDX-License-Identifier: Apache-2.0
"""Text helpers for frame producers: fit a string into a UTF-8 byte limit (pure, no I/O).

The protocol's string limits count UTF-8 bytes (protocol/README.md, limits table) and ``encode`` refuses a frame
that breaks one, so a producer cuts its text first:

* ``cut_utf8(text, max_bytes)`` - the longest prefix of ``text`` that fits ``max_bytes`` UTF-8 bytes, cut on a code
  point boundary (never inside a multi-byte sequence). A lone surrogate counts 3 bytes and is kept: ``encode``
  refuses it later.
* ``sanitize_card_text(text, max_bytes)`` - for text a card shows from an untrusted source (an approval's command,
  a clarify question): every bidi control in ``BIDI_CONTROLS`` becomes U+FFFD, so the text cannot reorder what the
  person reads, then ``cut_utf8``.
"""

from __future__ import annotations

from typing import Final

# U+061C ARABIC LETTER MARK, U+200E/U+200F LRM/RLM, U+202A-U+202E embeddings and overrides, U+2066-U+2069 isolates.
BIDI_CONTROLS: Final = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
REPLACEMENT: Final = "\ufffd"
_BIDI_TO_REPLACEMENT: Final = str.maketrans(dict.fromkeys(BIDI_CONTROLS, REPLACEMENT))
_CONTINUATION_MASK, _CONTINUATION = 0xC0, 0x80  # a UTF-8 continuation byte is 0b10xxxxxx


def cut_utf8(text: str, max_bytes: int) -> str:
    """The longest prefix of ``text`` of at most ``max_bytes`` UTF-8 bytes, ending on a code-point boundary."""
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    encoded = text.encode("utf-8", "surrogatepass")
    if len(encoded) <= max_bytes:
        return text
    end = max_bytes
    while end > 0 and encoded[end] & _CONTINUATION_MASK == _CONTINUATION:
        end -= 1  # encoded[end] continues the code point that starts before it: cut in front of that one
    return encoded[:end].decode("utf-8", "surrogatepass")


def sanitize_card_text(text: str, max_bytes: int) -> str:
    """``text`` with every bidi control replaced by U+FFFD, then cut to ``max_bytes`` UTF-8 bytes."""
    return cut_utf8(text.translate(_BIDI_TO_REPLACEMENT), max_bytes)
