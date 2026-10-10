# SPDX-License-Identifier: Apache-2.0
"""UTF-8 byte limits at their exact boundary with 3- and 4-byte characters, for every row of the limits table, and
measured on the decoded string whatever its JSON spelling (protocol/README.md, Limits and validation notes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses import envelope_rules
from glasses.envelope import FieldTooLong, decode
from glasses.envelope_rules import BYTE_LIMITS, S2D_ONLY, Violation, violation

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))
EMOJI = "\U0001f600"  # 4 UTF-8 bytes
EURO = "\u20ac"  # 3 UTF-8 bytes
TEXT_LIMITS = [row for row in BYTE_LIMITS if row[1] != "chunk"]  # base64 chunks are ASCII by pattern
IDS = [f"{frame_type}.{path}" for frame_type, path, _ in TEXT_LIMITS]


def load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((PROTOCOL / "fixtures" / name).read_text(encoding="utf-8"))
    return loaded


def frame_with(frame_type: str, path: str, text: str) -> dict[str, Any]:
    frame = load("valid/msg.out-long-ref.json" if path.startswith("longRef") else f"valid/{frame_type}.json")
    *parents, last = path.split(".")
    body = frame["body"]
    for part in parents:
        body = body[part]
    if last.endswith("[]"):
        body[last[:-2]] = ["ok", text]
    else:
        body[last] = text
    return frame


def texts(limit: int) -> list[tuple[str, bool]]:
    """(text, within the limit) pairs: the last byte fits or overflows, by 4- and 3-byte characters."""
    return [
        (EMOJI * (limit // 4) + "a" * (limit % 4), True),
        (EMOJI * (limit // 4) + "a" * (limit % 4 + 1), False),
        ("a" * (limit - 4) + EMOJI, True),
        ("a" * (limit - 3) + EMOJI, False),  # one character straddles the limit
        (EURO * (limit // 3) + "a" * (limit % 3), True),
        ("a" * (limit - 2) + EURO, False),
    ]


@pytest.mark.parametrize(("frame_type", "path", "limit"), TEXT_LIMITS, ids=IDS)
def test_byte_limit_boundary_with_multibyte_characters(frame_type: str, path: str, limit: int) -> None:
    from_device = frame_type not in S2D_ONLY
    for text, within in texts(limit):
        assert len(text.encode("utf-8")) == (limit if within else limit + 1)
        frame = frame_with(frame_type, path, text)
        assert VALIDATOR.is_valid(frame), (len(text), within)  # code points stay within maxLength
        assert violation(frame, from_device=from_device) is (None if within else Violation.TOO_MANY_BYTES)


DEVICE_LIMITS = [row for row in TEXT_LIMITS if row[0] not in S2D_ONLY]


@pytest.mark.parametrize("ensure_ascii", [True, False], ids=["escaped", "raw"])
@pytest.mark.parametrize(("frame_type", "path", "limit"), DEVICE_LIMITS, ids=[f"{t}.{p}" for t, p, _ in DEVICE_LIMITS])
def test_device_byte_limit_counts_the_decoded_string_in_either_spelling(
    frame_type: str, path: str, limit: int, ensure_ascii: bool
) -> None:
    at_limit = frame_with(frame_type, path, EMOJI * (limit // 4) + "a" * (limit % 4))
    assert decode(json.dumps(at_limit, ensure_ascii=ensure_ascii), VALIDATOR) == at_limit
    over = frame_with(frame_type, path, "a" * (limit - 3) + EMOJI)
    with pytest.raises(FieldTooLong):
        decode(json.dumps(over, ensure_ascii=ensure_ascii), VALIDATOR)


@pytest.mark.parametrize(("text", "within"), texts(envelope_rules.MSG_OUT_TEXT_WITH_LONGREF))
def test_msg_out_text_with_long_ref_boundary_with_multibyte_characters(text: str, within: bool) -> None:
    frame = load("valid/msg.out-long-ref.json")
    frame["body"]["text"] = text
    assert VALIDATOR.is_valid(frame)
    assert violation(frame, from_device=False) is (None if within else Violation.TOO_MANY_BYTES)


def test_every_list_item_is_measured_not_only_the_first() -> None:
    frame = load("valid/meeting.start.json")
    frame["body"]["prepTerms"] = ["ok", "fine", "a" * 97 + EMOJI]  # the last term is 101 B
    assert VALIDATOR.is_valid(frame)
    with pytest.raises(FieldTooLong):
        decode(json.dumps(frame), VALIDATOR)
