# SPDX-License-Identifier: Apache-2.0
"""WS server session (T1.07b), frame size and framing edges over a real socket: the cap is on bytes and on the
reassembled message (65 536 bytes is the largest accepted message, 65 537 closes with 1009), fragmented
and binary messages, a close frame, a first frame that is not ``hello``, and the extreme ``lastAckedSeq`` values."""

from __future__ import annotations

import json
import logging
import struct
from typing import Any

import pytest
from server_helpers import MSG_IN, PING, SCHEMA, wiring
from server_raw_ws import BINARY, CLOSE, TEXT, RawWs, frame_bytes, hello_bytes, reply, say_hello, session

CAP = 65_536  # welcome.limits.maxFrame: the largest accepted message
AUDIO = {
    "v": 1,
    "id": "01JABCDEFGHJKMNPQRSTVWXYZ0",
    "type": "audio.chunk",
    "ts": "2026-10-03T18:00:00Z",
    "body": {"clipId": "01JABCDEFGHJKMNPQRSTVWXYZ0", "chunk": "T2dnUwACAAAAAAAAAAA="},
}


def padded(frame: dict[str, Any], size: int) -> bytes:
    """*frame* as JSON of exactly *size* bytes (whitespace before the closing brace)."""
    raw = json.dumps(frame, separators=(",", ":"))
    assert len(raw) <= size
    return (raw[:-1] + " " * (size - len(raw)) + "}").encode()


@pytest.mark.parametrize("pieces", [1, 3])
def test_a_valid_frame_of_65535_bytes_is_dispatched_whole_or_in_fragments(tmp_path, pieces):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> dict[str, Any]:
        say_hello(ws)
        ws.send_message(TEXT, padded(MSG_IN, CAP - 1), pieces)
        ws.send(frame_bytes(TEXT, json.dumps(PING).encode()))
        return reply(ws)  # the pong: the msg.in before it was handled

    assert session(w, scenario)["type"] == "pong"
    assert w.frames == [(w.device.device_id, "msg.in")]


@pytest.mark.parametrize("pieces", [1, 2, 3])
def test_a_valid_frame_of_exactly_65536_bytes_the_advertised_maxframe_is_dispatched(tmp_path, pieces):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> dict[str, Any]:
        say_hello(ws)
        ws.send_message(TEXT, padded(MSG_IN, CAP), pieces)
        ws.send(frame_bytes(TEXT, json.dumps(PING).encode()))
        return reply(ws)

    assert session(w, scenario)["type"] == "pong"
    assert w.frames == [(w.device.device_id, "msg.in")]


@pytest.mark.parametrize("pieces", [1, 2, 3])
@pytest.mark.parametrize("opcode", [TEXT, BINARY], ids=["text", "binary"])
def test_a_message_of_65537_bytes_closes_with_1009_whole_or_in_fragments_and_is_not_dispatched(
    tmp_path, opcode, pieces
):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> int | None:
        say_hello(ws)
        ws.send_message(opcode, padded(MSG_IN, CAP + 1), pieces)
        return ws.close_code()

    assert session(w, scenario) == 1009
    assert w.frames == []


def test_the_cap_counts_bytes_not_characters(tmp_path):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> tuple[dict[str, Any], int | None]:
        say_hello(ws)
        euro = "\u20ac".encode()  # three bytes, one character
        ws.send(frame_bytes(TEXT, euro * 21_845))  # 65 535 bytes, 21 845 characters: under the cap
        under_cap = reply(ws)
        ws.send(frame_bytes(TEXT, euro * 21_846))  # 65 538 bytes, 21 846 characters
        return under_cap, ws.close_code()

    under_cap, code = session(w, scenario)
    assert under_cap["type"] == "error" and code == 1009


def test_an_audio_frame_over_the_codecs_32768_byte_audio_cap_gets_an_error_frame_and_the_session_stays(tmp_path):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> tuple[dict[str, Any], dict[str, Any]]:
        say_hello(ws)
        ws.send(frame_bytes(TEXT, padded(AUDIO, 32_769)))
        rejected = reply(ws)
        ws.send(frame_bytes(TEXT, json.dumps(PING).encode()))
        return rejected, reply(ws)

    rejected, alive = session(w, scenario)
    assert rejected["body"]["code"] == "frame_too_large" and alive["type"] == "pong"


def test_a_hello_sent_in_three_fragments_is_answered_by_a_welcome(tmp_path):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> dict[str, Any]:
        ws.send_message(TEXT, hello_bytes(lastAckedSeq=4), 3)
        return reply(ws)

    welcome = session(w, scenario)
    assert welcome["type"] == "welcome" and welcome["body"]["resumeFrom"] == 5 and SCHEMA.is_valid(welcome)


def test_a_binary_message_that_is_not_utf8_gets_an_error_frame_and_the_session_stays(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> tuple[dict[str, Any], dict[str, Any]]:
        say_hello(ws)
        ws.send(frame_bytes(BINARY, b"\xff\xfe\x00{"))
        rejected = reply(ws)
        ws.send(frame_bytes(TEXT, json.dumps(PING).encode()))
        return rejected, reply(ws)

    rejected, alive = session(w, scenario)
    assert rejected["type"] == "error" and SCHEMA.is_valid(rejected) and alive["type"] == "pong"
    assert "Traceback" not in caplog.text and w.frames == []


def test_a_close_frame_from_the_client_is_answered_and_leaves_no_stack_trace(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> int | None:
        say_hello(ws)
        ws.send(frame_bytes(CLOSE, struct.pack(">H", 1000)))
        return ws.close_code()

    assert session(w, scenario) == 1000
    assert "Traceback" not in caplog.text and "ERROR" not in caplog.text


def test_a_ping_before_hello_is_an_error_frame_and_a_later_hello_is_still_answered(tmp_path):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> tuple[dict[str, Any], dict[str, Any]]:
        ws.send(frame_bytes(TEXT, json.dumps(PING).encode()))
        refused = reply(ws)
        return refused, say_hello(ws)

    refused, welcome = session(w, scenario)
    assert refused["type"] == "error" and refused["body"]["code"] == "invalid_frame"
    assert welcome["type"] == "welcome" and w.frames == []


def test_a_third_hello_after_a_refused_second_one_is_still_refused(tmp_path):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> list[str]:
        say_hello(ws)
        return [say_hello(ws)["type"], say_hello(ws)["type"]]

    assert session(w, scenario) == ["error", "error"]


@pytest.mark.parametrize(("last_acked", "resume_from"), [(0, 1), (2**53 - 2, 2**53 - 1), (2**53 - 1, 2**53 - 1)])
def test_the_extreme_last_acked_seq_values_get_a_schema_valid_welcome_whose_resume_from_stays_in_range(
    tmp_path, caplog, last_acked, resume_from
):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> dict[str, Any]:
        ws.send(frame_bytes(TEXT, hello_bytes(lastAckedSeq=last_acked)))
        return reply(ws)

    welcome = session(w, scenario)
    assert SCHEMA.is_valid(welcome) and welcome["body"]["resumeFrom"] == resume_from  # 2**53 - 1 is the schema maximum
    assert "Traceback" not in caplog.text and "session failed" not in caplog.text
