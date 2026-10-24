# SPDX-License-Identifier: Apache-2.0
"""The outbox forgets a welcomed connection when its session ends (T1.249): ``Outbox.closed`` drops the live-link
entry only while it still belongs to that connection, and the server calls ``on_closed`` on every exit of the frame
loop, an error exit included."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
from outbox_helpers import A, Dev, FakeLink, answer, outbox, run, seqs
from outbox_session_helpers import device, rig, served
from server_helpers import MSG_IN, wiring

import test_adapter
from test_server_session import hello
from test_server_session import run as run_session

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument
DEVICES = [Dev(A)]


async def _until(done: Any) -> None:
    for _ in range(250):  # up to 5 s
        if done():
            return
        await asyncio.sleep(0.02)


def test_a_closed_session_drops_its_link_and_a_later_answer_is_only_stored(tmp_path):
    box, link = outbox(tmp_path), FakeLink()

    async def scenario() -> None:
        await box.hello(A, 0, link)
        box.closed(A, link)
        assert A not in box._links
        assert await box.send(lambda: DEVICES, "work", answer())
        await box.close()

    run(scenario())
    assert link.sent == [] and link.refusals == 0  # nothing written to the ended connection
    assert seqs(tmp_path, A) == [1]  # the row waits for the next hello


def test_the_end_of_an_older_session_keeps_the_link_of_a_newer_connection(tmp_path):
    box, old, new = outbox(tmp_path), FakeLink(), FakeLink()

    async def scenario() -> None:
        await box.hello(A, 0, old)
        await box.hello(A, 0, new)  # the newer connection was welcomed before the older session ended
        box.closed(A, old)
        assert box._links[A][0] is new
        await box.send(lambda: DEVICES, "work", answer())
        box.closed(A, FakeLink())  # an unknown connection changes nothing either
        assert box._links[A] == (new, 1)
        await box.close()

    run(scenario())
    assert [frame["seq"] for frame in new.sent] == [1] and old.sent == []


def test_closed_for_a_device_without_a_link_is_a_no_op(tmp_path):
    box = outbox(tmp_path)
    box.closed(A, FakeLink())
    assert box._links == {}
    run(box.close())


def _recorder() -> tuple[list[tuple[str, Any]], Any]:
    calls: list[tuple[str, Any]] = []
    return calls, lambda device_id, conn: calls.append((device_id, conn))


def test_on_closed_is_called_once_with_the_connection_when_the_client_closes(tmp_path):
    w = wiring(tmp_path)
    calls, on_closed = _recorder()
    seen: list[Any] = []

    async def on_hello(device_id: str, acked: int, conn: Any) -> tuple[int, list[str]]:
        seen.append(conn)
        return acked + 1, []

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> None:
        await hello(ws)
        await ws.close()
        await _until(lambda: calls)

    run_session(w, scenario, on_closed=on_closed, on_hello=on_hello)
    assert calls == [(w.device.device_id, seen[0])]


def test_on_closed_is_called_when_the_session_ends_on_a_failing_on_frame(tmp_path):
    w = wiring(tmp_path)
    calls, on_closed = _recorder()

    async def broken(device: Any, frame: Any) -> None:
        raise RuntimeError("on_frame failed")

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> int | None:
        await hello(ws)
        await ws.send_str(json.dumps(MSG_IN))
        await ws.receive()
        await _until(lambda: calls)
        return ws.close_code

    assert run_session(w, scenario, on_frame=broken, on_closed=on_closed) == 1011
    assert [device_id for device_id, _ in calls] == [w.device.device_id]


def test_a_device_that_disconnects_leaves_no_link_in_the_wired_outbox(adapter_module, tmp_path):
    r = rig(adapter_module, tmp_path)

    async def scenario() -> None:
        async with served(r) as port, aiohttp.ClientSession() as session:
            async with device(port, r, session, 0) as ws:
                assert (await ws.receive_json(timeout=5))["type"] == "welcome"
                assert r.w.device.device_id in r.box._links
            await _until(lambda: not r.box._links)
            assert r.box._links == {}

    run(scenario())
