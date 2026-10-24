# SPDX-License-Identifier: Apache-2.0
"""Inbound text through the adapter and the real WS server (T1.08): the adapter's frame router is the server's
``on_frame``, the error frames go out on the device's connection, and the stand-in Hermes base class records what
``handle_message`` got (``test_adapter``'s fakes). A real registry device connects over loopback."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType
from server_helpers import HELLO, MSG_IN, PING, SCHEMA, Wiring, serving, wiring

from glasses.server import CONNECTIONS, build_app

from test_adapter import FakeBaseAdapter, adapter_module, new_adapter, router_module  # noqa: F401 - fixtures by name

TEXT = "planted-text-9204"
EXTRA = {"lanes": ["assistant", "work", "gone"], "home_lane": "assistant"}
LANES = ("assistant", "work", "gone")
Scenario = Callable[[aiohttp.ClientWebSocketResponse], Awaitable[Any]]


@pytest.fixture
def module(adapter_module):  # noqa: F811 - the imported fixture
    return adapter_module


@pytest.fixture
def router(router_module):  # noqa: F811 - the imported fixture
    return router_module


def msg(lane: str | None = "work", frame_id: str = MSG_IN["id"]) -> str:
    frame = copy.deepcopy(MSG_IN)
    frame["id"], frame["body"]["text"] = frame_id, TEXT
    if lane is not None:
        frame["lane"] = lane
    return json.dumps(frame)


def rig(module: Any, tmp_path: Any, lanes: tuple[str, ...] = LANES) -> tuple[Any, Wiring]:
    adapter = new_adapter(module, EXTRA)
    adapter.unrouted_chats = {"glasses:gone"}  # the stand-in gateway's route to a profile it does not serve
    return adapter, wiring(tmp_path, lanes)


def run(adapter: Any, w: Wiring, scenario: Scenario, **changes: Any) -> Any:
    """The app with the adapter's router as ``on_frame`` (bound to the app's connection table, as ``connect()``
    does), one device connection that said hello, then *scenario*."""

    async def main() -> Any:
        changes.setdefault("on_frame", adapter.router.on_frame)
        app = build_app(w.deps(**changes))
        adapter.router.bind(app[CONNECTIONS], SCHEMA)
        try:
            async with serving(app) as port, aiohttp.ClientSession() as session:
                ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
                await ws.send_str(json.dumps(HELLO))
                assert (await ws.receive_json())["type"] == "welcome"
                try:
                    return await scenario(ws)
                finally:
                    await ws.close()
        finally:
            await adapter.disconnect()  # the gate's worker pool

    return asyncio.run(asyncio.wait_for(main(), 20))


async def until_pong(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
    """Send a ping and return every frame up to and including the pong (what the msg.in before it produced)."""
    await ws.send_str(json.dumps(PING))
    frames = [await ws.receive_json()]
    while frames[-1]["type"] != "pong":
        frames.append(await ws.receive_json())
    return frames


def test_an_allowed_msg_in_is_one_text_event_with_the_device_id_and_no_frame(module, router, tmp_path):
    adapter, w = rig(module, tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        await ws.send_str(msg("work"))
        await ws.send_str(msg(None, "01JABCDEFGHJKMNPQRSTVWXYZ1"))
        return await until_pong(ws)

    assert [f["type"] for f in run(adapter, w, scenario)] == ["pong"]
    work, home = adapter.handed_on
    assert (work.text, work.message_type, work.message_id) == (TEXT, router.MessageType.TEXT, MSG_IN["id"])
    assert work.user_id == w.device.device_id and work.source.user_id == w.device.device_id
    assert (work.source.chat_id, work.source.chat_type, home.source.chat_id) == (
        "glasses:work",
        "dm",
        "glasses:assistant",
    )


@pytest.mark.parametrize(
    ("lane", "device_lanes", "code"),
    [
        ("other", (*LANES, "other"), "lane_forbidden"),
        ("work", ("assistant",), "lane_forbidden"),
        ("gone", LANES, "lane_unrouted"),
    ],
    ids=["not in config lanes", "not in device lanes", "route to an unserved profile"],
)
def test_a_refused_lane_is_an_error_frame_with_corr_and_no_event(module, tmp_path, lane, device_lanes, code):
    adapter, w = rig(module, tmp_path, device_lanes)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        await ws.send_str(msg(lane))
        return await until_pong(ws)

    error, pong = run(adapter, w, scenario)
    assert error["type"] == "error" and error["body"]["code"] == code and error["corr"] == MSG_IN["id"]
    assert "seq" not in error and SCHEMA.is_valid(error) and pong["type"] == "pong" and adapter.handed_on == []


def test_no_text_event_controls_the_gateway_slash_commands_and_approval_words_included(module, tmp_path):
    adapter, w = rig(module, tmp_path)
    texts = ("/approve always", "/yolo", "yes", "always", "session", TEXT)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        for n, text in enumerate(texts):
            frame = json.loads(msg("work", f"01JABCDEFGHJKMNPQRSTVWX{n:03d}"))
            frame["body"]["text"] = text
            await ws.send_str(json.dumps(frame))
        return await until_pong(ws)

    assert [f["type"] for f in run(adapter, w, scenario)] == ["pong"]
    assert [event.text for event in adapter.handed_on] == list(texts)
    assert [event.allow_gateway_control for event in adapter.handed_on] == [False] * len(texts)


def test_a_repeated_id_is_handed_on_once(module, tmp_path):
    adapter, w = rig(module, tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        await ws.send_str(msg("work"))
        await ws.send_str(msg("assistant"))  # same envelope id on another lane: still the same message
        return await until_pong(ws)

    assert [f["type"] for f in run(adapter, w, scenario)] == ["pong"] and len(adapter.handed_on) == 1


def test_a_frame_other_than_msg_in_is_not_handed_on_and_gets_no_frame(module, tmp_path):
    adapter, w = rig(module, tmp_path)
    ack = {"v": 1, "id": MSG_IN["id"], "type": "ack", "ts": MSG_IN["ts"], "body": {"upToSeq": 1}}

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        await ws.send_str(json.dumps(ack))
        await ws.send_str(msg("work"))  # the ack took no dedupe entry for its id
        return await until_pong(ws)

    assert [f["type"] for f in run(adapter, w, scenario)] == ["pong"] and len(adapter.handed_on) == 1


def test_on_frame_does_not_wait_for_the_agent_turn_and_the_loop_keeps_reading(module, tmp_path, monkeypatch):
    adapter, w = rig(module, tmp_path)
    turns: list[asyncio.Task[None]] = []
    took: list[float] = []

    async def spawn_turn(self: Any, event: Any) -> None:  # as the pinned base: the turn is a background task
        self.handed_on.append(event)
        turns.append(asyncio.create_task(asyncio.sleep(2)))

    async def timed(device: Any, frame: Any) -> None:
        start = time.monotonic()
        await adapter.router.on_frame(device, frame)
        took.append(time.monotonic() - start)

    monkeypatch.setattr(FakeBaseAdapter, "handle_message", spawn_turn)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> tuple[float, bool]:
        start = time.monotonic()
        await ws.send_str(msg("work"))
        await until_pong(ws)
        elapsed, running = time.monotonic() - start, not turns[0].done()
        turns[0].cancel()
        return elapsed, running

    elapsed, running = run(adapter, w, scenario, on_frame=timed)
    assert running and took[0] < 0.05 and elapsed < 0.1, (took, elapsed)


def test_with_the_allow_all_gate_open_nothing_is_handed_on_and_nothing_is_sent(module, tmp_path, monkeypatch):
    adapter, w = rig(module, tmp_path)
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", "true")

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        await ws.send_str(msg("work"))
        return await until_pong(ws)

    assert [f["type"] for f in run(adapter, w, scenario)] == ["pong"] and adapter.handed_on == []


def test_a_revoked_device_gets_4403_and_its_msg_in_is_never_handed_on(module, tmp_path):
    adapter, w = rig(module, tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> tuple[WSMsgType, int | None]:
        w.revoked = True
        await ws.send_str(msg("work"))
        message = await ws.receive()
        return message.type, ws.close_code

    assert run(adapter, w, scenario) == (WSMsgType.CLOSE, 4403) and adapter.handed_on == []


def test_the_message_text_is_in_no_log_record_and_no_frame(module, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    sent: list[dict[str, Any]] = []

    async def every_path(ws: aiohttp.ClientWebSocketResponse) -> None:
        for lane in ("work", "work", "other", "gone", None):
            await ws.send_str(msg(lane))
            sent.extend(await until_pong(ws))

    adapter, w = rig(module, tmp_path)
    run(adapter, w, every_path)
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", "true")
    (tmp_path / "gate").mkdir()
    closed, w2 = rig(module, tmp_path / "gate")
    run(closed, w2, every_path)
    assert len(adapter.handed_on) == 1 and closed.handed_on == [] and len(sent) == 14
    assert TEXT not in caplog.text and TEXT not in json.dumps(sent)
    assert all(TEXT not in repr((r.msg, r.args, r.exc_text)) for r in caplog.records)
