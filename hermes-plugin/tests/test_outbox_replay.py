# SPDX-License-Identifier: Apache-2.0
"""Delivery and replay over the real WS server (T1.09b): once and in order across a dropped socket, the reconnect
hand-off (``welcome`` without ``seq``, then the queued frames), a refused send closing ``1008`` with the row kept,
the end-to-end path ``msg.in`` -> ``send()`` -> ``msg.out``, and a free loop while SQLite stalls."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType
from outbox_helpers import run, seqs
from outbox_session_helpers import Rig, device, rig, served, until_pong
from server_helpers import MSG_IN, SCHEMA

from glasses import connections, server_session
from glasses import outbox as outbox_module

from test_adapter import FakeBaseAdapter, adapter_module  # noqa: F401 - the fixture is used by name


@pytest.fixture
def module(adapter_module):  # noqa: F811 - the imported fixture
    return adapter_module


async def answers(r: Rig, *texts: str, chat_id: str = "glasses:work") -> None:
    for text in texts:
        result = await r.adapter.send(chat_id, text)
        assert result.success is True


def ack(up_to: int) -> str:
    return json.dumps({"v": 1, "id": MSG_IN["id"], "type": "ack", "ts": MSG_IN["ts"], "body": {"upToSeq": up_to}})


def test_five_answers_across_a_dropped_socket_arrive_once_and_in_order(module, tmp_path):
    r = rig(module, tmp_path)

    async def main() -> list[dict[str, Any]]:
        async with served(r) as port, aiohttp.ClientSession() as session:
            async with device(port, r, session, 0) as ws:
                await until_pong(ws, r.phone)  # welcomed
                await answers(r, "a1", "a2")
                await until_pong(ws, r.phone)  # both written; the socket drops before any ack
            await answers(r, "a3", "a4", "a5")
            async with device(port, r, session, 1) as ws:
                wire = await until_pong(ws, r.phone)
                await ws.send_str(ack(5))
                await until_pong(ws, r.phone)
            await asyncio.sleep(0.05)
        return wire

    wire = run(main())
    assert [f["type"] for f in wire] == ["welcome", "msg.out", "msg.out", "msg.out", "msg.out", "pong"]
    assert "seq" not in wire[0] and wire[0]["body"]["resumeFrom"] == 2 and [f["seq"] for f in wire[1:5]] == [2, 3, 4, 5]
    assert r.phone.texts() == ["a1", "a2", "a3", "a4", "a5"] and seqs(r.state, r.w.device.device_id) == []


def test_a_reconnect_replays_2_to_4_after_welcome_and_an_ack_of_1_purges_nothing_queued(module, tmp_path):
    r = rig(module, tmp_path)

    async def main() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async with served(r) as port, aiohttp.ClientSession() as session:
            await answers(r, "a1", "a2", "a3", "a4")  # offline: all four queued
            async with device(port, r, session, 1) as ws:
                first = await until_pong(ws, r.phone)
                await ws.send_str(ack(1))
                await until_pong(ws, r.phone)
            async with device(port, r, session, 1) as ws:
                second = await until_pong(ws, r.phone)
        return first, second

    first, second = run(main())
    assert [f.get("seq") for f in first] == [None, 2, 3, 4, None] and first[0]["type"] == "welcome"
    assert [f.get("seq") for f in second] == [None, 2, 3, 4, None]  # still queued: nothing was acked past 1
    assert second[0]["id"] != first[0]["id"] and second[0]["type"] == "welcome" and "seq" not in second[0]
    assert sorted(r.phone.seen) == [2, 3, 4]


def test_a_live_answer_after_the_replay_is_numbered_after_it_and_never_repeats_it(module, tmp_path):
    r = rig(module, tmp_path)

    async def main() -> list[dict[str, Any]]:
        async with served(r) as port, aiohttp.ClientSession() as session:
            await answers(r, "a1", "a2")
            async with device(port, r, session, 0) as ws:
                await until_pong(ws, r.phone)
                await answers(r, "a3")
                return await until_pong(ws, r.phone)

    assert [(f["type"], f.get("seq")) for f in run(main())] == [("msg.out", 3), ("pong", None)]
    assert [f.get("seq") for f in r.phone.wire] == [None, 1, 2, None, 3, None]


def test_a_refused_send_closes_1008_keeps_the_row_and_it_arrives_after_a_reconnect(module, tmp_path, monkeypatch):
    r = rig(module, tmp_path)
    real_send = connections.Connection.send

    def full_for_answers(self: connections.Connection, text: str) -> bool:
        return False if '"msg.out"' in text else real_send(self, text)  # the queue is full when the answer comes

    async def main() -> tuple[int | None, list[dict[str, Any]]]:
        async with served(r) as port, aiohttp.ClientSession() as session:
            async with device(port, r, session, 0) as ws:
                await until_pong(ws, r.phone)
                monkeypatch.setattr(connections.Connection, "send", full_for_answers)
                await answers(r, "kept")
                closed = await ws.receive(timeout=5)
                code = ws.close_code if closed.type in (WSMsgType.CLOSE, WSMsgType.CLOSED) else None
            monkeypatch.setattr(connections.Connection, "send", real_send)
            async with device(port, r, session, 0) as ws:
                return code, await until_pong(ws, r.phone)

    code, wire = run(main())
    assert code == 1008 and [(f["type"], f.get("seq")) for f in wire] == [
        ("welcome", None),
        ("msg.out", 1),
        ("pong", None),
    ]
    assert r.phone.texts() == ["kept"]


def test_a_refused_replayed_frame_closes_1008_and_no_later_seq_follows_it(module, tmp_path, monkeypatch):
    r = rig(module, tmp_path)
    real_send = connections.Connection.send

    def full_at_seq_2(self: connections.Connection, text: str) -> bool:
        return False if '"seq":2' in text.replace(" ", "") else real_send(self, text)

    async def main() -> tuple[int | None, list[Any]]:
        async with served(r) as port, aiohttp.ClientSession() as session:
            await answers(r, "a", "b", "c")  # queued while the device is offline
            monkeypatch.setattr(connections.Connection, "send", full_at_seq_2)
            seen: list[Any] = []
            async with device(port, r, session, 0) as ws:
                while (got := await ws.receive(timeout=5)).type is WSMsgType.TEXT:
                    seen.append(json.loads(got.data).get("seq"))
                return ws.close_code, seen

    code, seen = run(main())
    assert code == 1008 and seen == [None, 1] and seqs(r.state, r.w.device.device_id) == [1, 2, 3]


def test_reply_on_a_refused_frame_ends_the_connection():
    class Conn:
        refusals = 0

        def send(self, _text: str) -> bool:
            return False  # a full or halted queue

        def refused(self) -> None:
            self.refusals += 1

    conn = Conn()
    pong = {"v": 1, "id": MSG_IN["id"], "type": "pong", "ts": MSG_IN["ts"], "body": {}}
    server_session._reply(conn, pong, SimpleNamespace(schema=SCHEMA))  # type: ignore[arg-type]
    assert conn.refusals == 1


def test_a_connection_that_refuses_twice_starts_one_close_and_the_second_is_a_debug_line(caplog):
    async def main() -> tuple[int, list[str]]:
        class WS:
            closes: list[int] = []

            async def send_str(self, _t: str) -> None:
                await asyncio.sleep(10)

            async def close(self, code: int) -> bool:
                WS.closes.append(code)
                return True

        conn = connections.Connection("0123456789abcdef", WS(), None)  # type: ignore[arg-type]
        conn.refused()
        conn.refused()
        await asyncio.sleep(0.05)
        return len(WS.closes), WS.closes

    caplog.set_level("DEBUG")
    count, codes = run(main())
    assert (count, codes) == (1, [1008]) and "frame not queued (connection ending)" in caplog.text


def test_end_to_end_a_msg_in_answered_by_send_reaches_the_phone_with_corr_and_chat_id(module, tmp_path, monkeypatch):
    r = rig(module, tmp_path)
    turns: list[asyncio.Task[Any]] = []

    async def agent_turn(self: Any, event: Any) -> None:  # the base class spawns the turn; the turn ends in send()
        async def turn() -> None:
            await self.send(event.source.chat_id, "the answer", reply_to=event.message_id)

        turns.append(asyncio.create_task(turn()))

    monkeypatch.setattr(FakeBaseAdapter, "handle_message", agent_turn)
    frame = copy.deepcopy(MSG_IN)
    frame["lane"] = "work"

    async def main() -> list[dict[str, Any]]:
        async with served(r) as port, aiohttp.ClientSession() as session, device(port, r, session, 0) as ws:
            await until_pong(ws, r.phone)
            await ws.send_str(json.dumps(frame))
            got = await ws.receive_json(timeout=5)
            await asyncio.gather(*turns)
            return [got]

    (out,) = run(main())
    assert out["type"] == "msg.out" and out["corr"] == MSG_IN["id"] and out["body"]["chatId"] == "glasses:work"
    assert out["body"]["text"] == "the answer" and out["seq"] == 1


def test_a_stalled_store_does_not_stall_the_session_a_ping_is_answered_within_100_ms(module, tmp_path, monkeypatch):
    r = rig(module, tmp_path)
    real_add = outbox_module.Store._add

    def stalled(self: Any, db: Any, device_id: str, build: Any) -> Any:
        time.sleep(2)
        return real_add(self, db, device_id, build)

    async def main() -> float:
        async with served(r) as port, aiohttp.ClientSession() as session, device(port, r, session, 0) as ws:
            await until_pong(ws, r.phone)
            monkeypatch.setattr(outbox_module.Store, "_add", stalled)
            sending = asyncio.ensure_future(answers(r, "slow"))
            await ws.send_str(ack(0))  # an ack queued behind the stalled call does not hold the frame loop
            await asyncio.sleep(0.05)
            start = time.monotonic()
            await until_pong(ws, r.phone)
            took = time.monotonic() - start
            await sending
            return took

    assert run(main()) < 0.1
