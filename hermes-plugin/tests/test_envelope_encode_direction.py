# SPDX-License-Identifier: Apache-2.0
"""``encode`` sends only what the server may send: a type only the device sends is refused (reason ``direction``) and
nothing is emitted; every S→D and both-ways frame encodes. An oversized frame is refused before any parsing, and a
device-only frame by its direction before the server-side rules run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses.envelope import decode
from glasses.envelope_encode import EncodeRefused, encode
from glasses.envelope_rules import D2S_ONLY
from glasses.envelope_scan import AUDIO_TYPES, FRAME_CAP, cap_for
from glasses.generated.protocol_types import FRAME_TYPES

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
VALID = PROTOCOL / "fixtures" / "valid"
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))


def load_path(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def fixtures(*, device_only: bool) -> list[Any]:
    paths = sorted(VALID.glob("*.json"))
    return [pytest.param(p, id=p.name) for p in paths if (load_path(p)["type"] in D2S_ONLY) is device_only]


def refused(frame: Any) -> EncodeRefused:
    sink: list[str] = []
    with pytest.raises(EncodeRefused) as caught:
        sink.append(encode(frame, VALIDATOR))
    assert sink == []  # nothing emitted
    assert caught.value.__context__ is None
    return caught.value


@pytest.mark.parametrize("path", fixtures(device_only=True))
def test_device_only_frame_is_refused_by_direction(path: Path) -> None:
    error = refused(load_path(path))
    assert error.reason == "direction"
    assert str(error) == "frame not encoded: direction"


def test_every_device_only_type_has_a_refused_fixture() -> None:
    assert len(D2S_ONLY) == 13
    assert {load_path(p.values[0])["type"] for p in fixtures(device_only=True)} == D2S_ONLY


@pytest.mark.parametrize("path", fixtures(device_only=False))
def test_server_and_both_ways_fixture_encodes_to_itself(path: Path) -> None:
    frame = load_path(path)
    assert json.loads(encode(frame, VALIDATOR)) == frame


@pytest.mark.parametrize("name", ["ping.json", "pong.json", "error.json"])
def test_both_ways_frame_round_trips_through_decode(name: str) -> None:
    frame = load_path(VALID / name)
    if frame["type"] == "error":
        frame["body"]["message"] = "Привет, \U0001f600 «ok»"
    text = encode(frame, VALIDATOR)
    if frame["type"] == "error":
        assert "Привет" in text  # UTF-8, not \u escapes: the byte limits measure what is sent
    assert decode(text, VALIDATOR) == frame


def test_frame_over_the_cap_is_refused_before_parsing_and_validation() -> None:
    seen: list[object] = []

    class Spy:
        def is_valid(self, instance: object) -> bool:
            seen.append(instance)
            return True

    frame = load_path(VALID / "msg.out.json")
    frame["body"]["text"] = "x" * FRAME_CAP
    frame["body"].update({1: 0, "1": 0})  # one key twice in the text: the parse-back alone would say not_json
    assert refused_by(frame, Spy()) == "frame_too_large"
    assert seen == []  # the size alone decided: no parse-back, no schema run


def test_device_only_frame_over_a_byte_limit_is_refused_by_direction_not_by_the_rules() -> None:
    frame = load_path(VALID / "msg.in.json")
    frame["body"]["text"] = "ж" * 2000  # schema-valid, 4000 UTF-8 bytes: over the msg.in.text byte limit
    assert refused(frame).reason == "direction"


def test_every_type_the_server_sends_has_the_full_frame_cap() -> None:
    # Why the per-type cap check after validation never fires today: only device-only types have a smaller cap.
    assert AUDIO_TYPES <= D2S_ONLY
    assert all(cap_for(frozenset({t})) == FRAME_CAP for t in set(FRAME_TYPES) - D2S_ONLY)


def refused_by(frame: Any, schema: Any) -> str:
    with pytest.raises(EncodeRefused) as caught:
        encode(frame, schema)
    return caught.value.reason
