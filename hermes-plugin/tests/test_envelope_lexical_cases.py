# SPDX-License-Identifier: Apache-2.0
"""T1.105: lexical cases of protocol/fixtures/lexical.json through the server codec.

The phone's ``decodeEnvelope`` runs the same file (EnvelopeLexicalTest.kt): a bare value inside an open object that is
not ``true``/``false``/``null``/a finite number, a raw control character in a string, whitespace that JSON does not
have and nesting over 35 brackets are refused here as ``invalid_json`` (or ``frame_too_deep``); the accepted cases
decode. The two decoders may differ only where the README lists a gap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from glasses.envelope import FrameRejected, decode

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
CASES: dict[str, Any] = json.loads((PROTOCOL / "fixtures" / "lexical.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))


def frame(frame_type: str, body: str) -> str:
    head = '{"v":1,"id":"01JABCDEFGHJKMNPQRSTVWXYZ0","type":"' + frame_type + '","ts":"2026-10-03T18:00:00Z","body":'
    return head + body + "}"


def result(output: str) -> str:
    return frame("skill.result", '{"callId":"01JABCDEFGHJKMNPQRSTVWXYZ0","status":"ok","output":' + output + "}")


def manifest(item: str) -> str:
    return frame("skill.manifest", '{"manifestVersion":1,"skills":[' + item + "]}")


POSITIONS = {
    "output": lambda value: result('{"n":' + value + "}"),
    "item": lambda value: manifest('{"name":"device.status","n":' + value + "}"),
}


def code(text: str | bytes) -> str:
    try:
        decode(text, VALIDATOR)
    except FrameRejected as rejected:
        return rejected.code
    return "accepted"


def both(text: str) -> set[str]:
    return {code(text), code(text.encode("utf-8"))}


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("value", CASES["bareValues"]["refused"])
def test_a_bare_value_that_is_not_json_is_invalid_json(position: str, value: str) -> None:
    assert both(POSITIONS[position](value)) == {"invalid_json"}


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("value", CASES["bareValues"]["accepted"])
def test_a_bare_json_value_in_an_open_object_is_accepted(position: str, value: str) -> None:
    assert both(POSITIONS[position](value)) == {"accepted"}


def test_an_integer_literal_has_at_most_the_digits_of_the_limit() -> None:
    limit = CASES["integerDigits"]
    assert both(POSITIONS["output"]("1" * limit)) == {"accepted"}
    assert both(POSITIONS["output"]("-" + "1" * limit)) == {"accepted"}
    assert both(POSITIONS["output"]("1" * (limit + 1))) == {"invalid_json"}
    assert both(POSITIONS["output"]("-" + "1" * (limit + 1))) == {"invalid_json"}


def _in_text(char: str) -> str:
    return frame("msg.in", '{"text":"a' + char + 'b","origin":"ptt"}')


def _in_key(char: str) -> str:
    return POSITIONS["output"]('1,"a' + char + 'b":2')


@pytest.mark.parametrize("build", [_in_text, _in_key])
@pytest.mark.parametrize("point", CASES["stringCharacters"]["refused"])
def test_a_raw_control_character_in_a_string_is_invalid_json(build: Any, point: int) -> None:
    assert both(build(chr(point))) == {"invalid_json"}


@pytest.mark.parametrize("build", [_in_text, _in_key])
@pytest.mark.parametrize("point", CASES["stringCharacters"]["accepted"])
def test_a_raw_character_from_space_up_is_accepted_in_a_string(build: Any, point: int) -> None:
    assert both(build(chr(point))) == {"accepted"}


def _escaped(escape: str) -> str:
    return frame("msg.in", '{"text":"a' + escape + 'b","origin":"ptt"}')


@pytest.mark.parametrize("escape", CASES["escapes"]["refused"])
def test_an_escape_sequence_that_json_does_not_have_is_invalid_json(escape: str) -> None:
    assert both(_escaped(escape)) == {"invalid_json"}


@pytest.mark.parametrize("escape", CASES["escapes"]["accepted"])
def test_an_escape_sequence_of_json_is_accepted(escape: str) -> None:
    assert both(_escaped(escape)) == {"accepted"}


def _spaced(char: str) -> str:
    return result(char + '{"n":1}')


@pytest.mark.parametrize("point", CASES["whitespace"]["refused"])
def test_whitespace_that_json_does_not_have_is_invalid_json(point: int) -> None:
    assert both(_spaced(chr(point))) == {"invalid_json"}


@pytest.mark.parametrize("point", CASES["whitespace"]["accepted"])
def test_the_four_whitespace_characters_of_json_are_accepted(point: int) -> None:
    assert both(_spaced(chr(point))) == {"accepted"}


def _nested(brackets: int) -> str:
    """A manifest whose raw text opens ``brackets`` brackets at once: envelope, body, skills, item and the arrays."""
    inner = "[" * (brackets - 4) + "1" + "]" * (brackets - 4)
    return manifest('{"name":"device.status","n":' + inner + "}")


@pytest.mark.parametrize(("brackets", "expected"), [(34, "accepted"), (35, "accepted"), (36, "frame_too_deep")])
def test_nesting_over_35_brackets_is_refused_before_parsing(brackets: int, expected: str) -> None:
    assert both(_nested(brackets)) == {expected}


@pytest.mark.parametrize(("brackets", "expected"), [(35, "invalid_frame"), (36, "frame_too_deep")])
def test_the_prescan_counts_every_bracket_not_only_those_of_an_open_field(brackets: int, expected: str) -> None:
    assert both("[" * brackets + "]" * brackets) == {expected}


def test_a_frame_of_100000_open_brackets_is_refused() -> None:
    assert code("[" * 100_000) in {"frame_too_deep", "frame_too_large"}
