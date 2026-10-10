# SPDX-License-Identifier: Apache-2.0
"""No rejection of envelope decode echoes its input: one case per rejection kind and per cause inside a kind, each
with a marker in the payload; the error frame, the exception text, its args and its chain hold none of it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses import envelope_scan
from glasses.envelope import (
    FieldTooLong,
    FrameRejected,
    FrameTooDeep,
    FrameTooLarge,
    InvalidFrame,
    InvalidJson,
    InvalidSeq,
    InvalidText,
    decode,
    error_frame,
)

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))
MARKER = "ECHO-MARKER-91c2"


def load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((PROTOCOL / "fixtures" / name).read_text(encoding="utf-8"))
    return loaded


def msg_in(text: str = MARKER, **extra: object) -> dict[str, Any]:
    frame = load("valid/msg.in.json")
    frame["body"]["text"] = text
    return frame | extra


def dumps(frame: dict[str, Any]) -> str:
    return json.dumps(frame)


def audio_over_cap_with_escaped_type() -> str:
    """An audio frame of cap + 1 bytes whose type only the parser reads (the cap check after parsing refuses it)."""
    raw = dumps(load("valid/audio.chunk.json") | {MARKER: 1}).replace('"audio.chunk"', '"audio\\u002echunk"', 1)
    return raw[:-1] + " " * (envelope_scan.AUDIO_FRAME_CAP + 1 - len(raw)) + "}"


def deep_list(levels: int) -> object:
    value: object = 1
    for _ in range(levels):
        value = [value]
    return value


def cases() -> list[tuple[str, bytes | str, type[FrameRejected]]]:
    over_cap = dumps(msg_in()) + " " * envelope_scan.FRAME_CAP
    feb_29 = msg_in(ts="2027-02-29T00:00:00Z")
    skill = load("valid/skill.result.json")
    skill["body"]["output"] = {MARKER: deep_list(32)}  # output 1 + 32 lists = 33
    error: dict[str, Any] = load("valid/error.json") | {"seq": 1}
    error["body"]["message"] = MARKER
    return [
        ("over the frame cap, text", over_cap, FrameTooLarge),
        ("over the frame cap, bytes", over_cap.encode("utf-8"), FrameTooLarge),
        ("audio over its cap, parsed type", audio_over_cap_with_escaped_type(), FrameTooLarge),
        ("pre-scan depth", MARKER + "[" * 64, FrameTooDeep),
        ("per-field depth", dumps(skill), FrameTooDeep),
        ("syntax", '{"' + MARKER + '":', InvalidJson),
        ("NaN", dumps(msg_in())[:-1] + ', "x": NaN}', InvalidJson),
        ("duplicate key", dumps(msg_in())[:-1] + f', "{MARKER}": 1, "{MARKER}": 2}}', InvalidJson),
        ("integer over the digit limit", dumps(msg_in())[:-1] + ', "x": ' + "9" * 5000 + "}", InvalidJson),
        ("trailing data", dumps(msg_in()) + MARKER, InvalidJson),
        ("not UTF-8", dumps(msg_in()).encode("utf-8").replace(MARKER.encode(), MARKER.encode() + b"\xff"), InvalidText),
        ("lone surrogate", dumps(msg_in()).replace(MARKER, MARKER + "\\udfff"), InvalidText),
        ("schema", dumps(msg_in(extra=MARKER)), InvalidFrame),
        ("Feb 29", dumps(feb_29), InvalidFrame),
        ("seq from the device", dumps(error), InvalidSeq),
        ("byte limit", dumps(msg_in(MARKER + "\u044f" * 1000)), FieldTooLong),
    ]


CASES = cases()


@pytest.mark.parametrize(("raw", "kind"), [case[1:] for case in CASES], ids=[case[0] for case in CASES])
def test_no_rejection_echoes_its_input(raw: bytes | str, kind: type[FrameRejected]) -> None:
    assert MARKER in (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    with pytest.raises(FrameRejected) as caught:
        decode(raw, VALIDATOR)
    rejected = caught.value
    assert type(rejected) is kind
    frame = error_frame(rejected, "01JABCDEFGHJKMNPQRSTVWXYZ1", "2026-10-03T18:00:01Z")
    assert frame["body"] == {"code": kind.code, "message": kind.message}
    assert VALIDATOR.is_valid(frame)
    for text in (json.dumps(frame), str(rejected), repr(rejected), repr(rejected.args), repr(vars(rejected))):
        assert MARKER not in text
    assert rejected.args == (kind.message,)
    assert rejected.__cause__ is None
    assert rejected.__context__ is None
