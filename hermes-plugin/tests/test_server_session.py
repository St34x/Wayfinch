# SPDX-License-Identifier: Apache-2.0
"""WS server session (T1.07b): hello/welcome, ping/pong, the frame loop, caps, and the revocation check per frame."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType
from server_helpers import ERROR, HELLO, MSG_IN, PING, SCHEMA, Wiring, app_for, serving, wiring

from glasses.server import build_app

MARKER = "ECHO-MARKER-7f3a"


def run(w: Wiring, scenario: Callable[[aiohttp.ClientWebSocketResponse], Awaitable[Any]], **changes: Any) -> Any:
    async def main() -> Any:
        async with serving(app_for(w, **changes)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            try:
                return await scenario(ws)
            finally:
                await ws.close()

    return asyncio.run(asyncio.wait_for(main(), 20))


async def hello(ws: aiohttp.ClientWebSocketResponse, **body: Any) -> dict[str, Any]:
    frame = copy.deepcopy(HELLO)
    frame["body"].update(body)
    await ws.send_str(json.dumps(frame))
    reply: dict[str, Any] = await ws.receive_json()
    return reply


def test_hello_is_answered_by_a_schema_valid_welcome_with_the_devices_lanes_and_the_clip_cap(tmp_path):
    w = wiring(tmp_path, ("main", "work"))

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        return await hello(ws, lastAckedSeq=6)

    welcome = run(w, scenario, max_audio_sec=12)
    assert SCHEMA.is_valid(welcome), welcome
    assert "seq" not in welcome  # outside the outbox numbering (T1.09a)
    body = welcome["body"]
    assert welcome["type"] == "welcome" and body["deviceId"] == w.device.device_id
    assert body["lanes"] == ["main", "work"]
    assert body["limits"] == {"maxFrame": 65536, "maxAudioSec": 12}
    assert body["resumeFrom"] == 7 and w.frames == []  # hello is the server's, never dispatched


@pytest.mark.parametrize("clip_cap", [0, 31, -1])
def test_a_clip_cap_outside_1_to_30_is_refused_when_the_app_is_built(tmp_path, clip_cap):
    with pytest.raises(ValueError, match="max_audio_sec"):
        build_app(wiring(tmp_path).deps(max_audio_sec=clip_cap))


def test_a_device_with_32_lanes_gets_32_lanes_in_the_welcome(tmp_path):
    w = wiring(tmp_path, tuple(f"lane{i}" for i in range(32)))

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        return await hello(ws)

    welcome = run(w, scenario)
    assert len(welcome["body"]["lanes"]) == 32 and SCHEMA.is_valid(welcome)


def test_ping_is_answered_by_pong_without_seq_and_is_not_dispatched(tmp_path):
    w = wiring(tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        await hello(ws)
        await ws.send_str(json.dumps(PING))
        reply: dict[str, Any] = await ws.receive_json()
        return reply

    pong = run(w, scenario)
    assert pong["type"] == "pong" and "seq" not in pong and SCHEMA.is_valid(pong)
    assert w.frames == []


def test_a_65537_byte_message_closes_the_socket_with_1009(tmp_path):
    w = wiring(tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> int | None:
        await hello(ws)
        await ws.send_str(" " * 65_537)
        message = await ws.receive()
        assert message.type is WSMsgType.CLOSE
        return ws.close_code

    assert run(w, scenario) == 1009


def padded(frame: dict[str, Any], size: int) -> str:
    raw = json.dumps(frame, separators=(",", ":"))
    return raw[:-1] + " " * (size - len(raw)) + "}"


AUDIO = {"v": 1, "id": MSG_IN["id"], "type": "audio.chunk", "ts": MSG_IN["ts"], "body": {}}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (MARKER + "{not json", "invalid_json"),
        (json.dumps({**MSG_IN, "seq": 3, "body": {**MSG_IN["body"], "text": MARKER}}), "invalid_frame"),
        (json.dumps({**ERROR, "seq": 3}), "invalid_seq"),
        (padded(AUDIO, 40_000), "frame_too_large"),
        (json.dumps({"v": 1, "type": MARKER}), "invalid_frame"),
    ],
    ids=["malformed", "seq-on-d2s", "seq-on-d2s-error", "over-cap-audio", "unknown-type"],
)
def test_a_rejected_frame_gets_an_error_frame_without_echo_and_the_session_stays_up(tmp_path, payload, code):
    w = wiring(tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> tuple[dict[str, Any], dict[str, Any]]:
        await hello(ws)
        await ws.send_str(payload)
        rejected: dict[str, Any] = await ws.receive_json()
        await ws.send_str(json.dumps(PING))
        alive: dict[str, Any] = await ws.receive_json()
        return rejected, alive

    rejected, alive = run(w, scenario)
    assert rejected["type"] == "error" and rejected["body"]["code"] == code and "seq" not in rejected
    assert SCHEMA.is_valid(rejected) and MARKER not in json.dumps(rejected)
    assert alive["type"] == "pong" and w.frames == []


def test_a_valid_frame_reaches_on_frame_once_the_device_said_hello(tmp_path):
    w = wiring(tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> None:
        await hello(ws)
        await ws.send_str(json.dumps(MSG_IN))
        await ws.send_str(json.dumps(PING))
        await ws.receive_json()  # the pong: msg.in was handled before it

    run(w, scenario)
    assert w.frames == [(w.device.device_id, "msg.in")]


@pytest.mark.parametrize("first", ["msg.in", "second hello"])
def test_a_frame_before_hello_and_a_second_hello_are_errors_and_not_dispatched(tmp_path, first):
    w = wiring(tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        if first == "second hello":
            await hello(ws)
            return await hello(ws)
        await ws.send_str(json.dumps(MSG_IN))
        reply: dict[str, Any] = await ws.receive_json()
        return reply

    reply = run(w, scenario)
    assert reply["type"] == "error" and reply["body"]["code"] == "invalid_frame" and w.frames == []


def test_after_is_revoked_flips_the_next_frame_is_not_dispatched_not_acknowledged_and_the_socket_closes_4403(tmp_path):
    w = wiring(tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> tuple[WSMsgType, int | None]:
        await hello(ws)
        await ws.send_str(json.dumps(MSG_IN))
        await ws.send_str(json.dumps(PING))
        await ws.receive_json()  # pong: the frame before the flip was served
        w.revoked = True
        await ws.send_str(json.dumps(MSG_IN))
        message = await ws.receive()  # no ack, no pong: the close frame
        return message.type, ws.close_code

    assert run(w, scenario) == (WSMsgType.CLOSE, 4403)
    assert w.frames == [(w.device.device_id, "msg.in")]  # only the frame from before the flip


@pytest.mark.parametrize("answer", [None, "yes", RuntimeError("x")], ids=["none", "str", "raises"])
def test_a_failing_revocation_check_drops_the_frame_unacknowledged_and_keeps_the_session(tmp_path, answer, caplog):
    w = wiring(tmp_path)
    caplog.set_level(logging.DEBUG)
    state = {"broken": False}

    def check(_id: str) -> Any:
        if state["broken"]:
            if isinstance(answer, Exception):
                raise answer
            return answer
        return False

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> None:
        await hello(ws)
        state["broken"] = True
        await ws.send_str(json.dumps(MSG_IN))
        await ws.send_str(json.dumps(PING))
        message = await ws.receive(timeout=0.5)  # nothing comes back: neither an ack nor a pong
        assert message.type is not WSMsgType.TEXT, message.data

    with pytest.raises(TimeoutError):
        run(w, scenario, is_revoked=check)
    assert w.frames == [] and "Traceback" not in caplog.text


def test_a_failing_on_frame_ends_that_session_with_1011_and_no_stack_trace(tmp_path, caplog):
    w = wiring(tmp_path)
    caplog.set_level(logging.DEBUG)

    async def broken(device: Any, frame: Any) -> None:
        raise RuntimeError(MARKER)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> int | None:
        await hello(ws)
        await ws.send_str(json.dumps(MSG_IN))
        await ws.receive()
        return ws.close_code

    assert run(w, scenario, on_frame=broken) == 1011
    assert "RuntimeError" in caplog.text and MARKER not in caplog.text and "Traceback" not in caplog.text


def test_a_client_that_vanishes_mid_session_leaves_no_stack_trace_in_the_log(tmp_path, caplog):
    w = wiring(tmp_path)
    caplog.set_level(logging.DEBUG)

    async def main() -> None:
        async with serving(app_for(w)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await hello(ws)
            ws._writer.transport.abort()  # the TCP connection dies without a close frame
            await asyncio.sleep(0.3)

    asyncio.run(asyncio.wait_for(main(), 20))
    assert "Traceback" not in caplog.text and "ERROR" not in caplog.text
