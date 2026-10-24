# SPDX-License-Identifier: Apache-2.0
"""T1.09a: ``welcome`` is outside the outbox numbering, so it never carries ``seq`` in either direction.

The server sends it without ``seq`` (the schema and ``violation`` refuse one) and refuses a ``welcome`` that arrives
from the device: with no ``seq`` to fail on, the direction rule of ``envelope_rules`` is the only thing that does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses.envelope import InvalidFrame, InvalidSeq, decode
from glasses.envelope_encode import EncodeRefused, encode
from glasses.envelope_rules import S2D_ONLY, UNNUMBERED_S2D, Violation, violation

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))


def welcome() -> dict[str, Any]:
    frame: dict[str, Any] = json.loads((PROTOCOL / "fixtures/valid/welcome.json").read_text(encoding="utf-8"))
    return frame


def test_welcome_is_the_one_unnumbered_server_to_device_type() -> None:
    assert {"welcome"} == UNNUMBERED_S2D and UNNUMBERED_S2D <= S2D_ONLY


def test_the_server_sends_a_welcome_without_seq_and_refuses_one_with_seq() -> None:
    frame = welcome()
    assert "seq" not in frame and violation(frame, from_device=False) is None
    assert json.loads(encode(frame, VALIDATOR)) == frame
    frame["seq"] = 1
    assert violation(frame, from_device=False) is Violation.SEQ
    with pytest.raises(EncodeRefused) as caught:
        encode(frame, VALIDATOR)
    assert caught.value.reason == "schema"  # the schema refuses it before the rules are asked


@pytest.mark.parametrize("with_seq", [False, True])
def test_a_welcome_from_the_device_is_refused_with_or_without_seq(with_seq: bool) -> None:
    frame = welcome()
    if with_seq:
        frame["seq"] = 1
    expected = InvalidFrame if with_seq else InvalidSeq  # with seq: the schema; without: the direction rule
    with pytest.raises(expected):
        decode(json.dumps(frame), VALIDATOR)
    assert violation(welcome(), from_device=True) is Violation.SEQ
