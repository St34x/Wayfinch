# SPDX-License-Identifier: Apache-2.0
"""WS server connection table (T1.07c): one connection per device (a newer one replaces the older with 4409), one
send queue and one writer task per connection, ``close_device`` with a queued burst, and shutdown with 1001."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from server_helpers import HELLO, MSG_IN, PING, Wiring, app_for, serving, wiring
from ws_raw_helpers import CLOSE, TEXT, RawClient, parse_frames

from glasses.connections import QUEUE_MAX, Connection
from glasses.server import CONNECTIONS


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 30))  # type: ignore[arg-type]


async def raw_session(port: int, w: Wiring, rcvbuf: int = 0) -> RawClient:
    """A raw client past ``hello``/``welcome`` (so the server has registered the connection)."""
    client = await RawClient.connect(port, w.headers(), rcvbuf)
    await client.send_json(HELLO)
    assert json.loads((await client.recv_frames(1))[0][1])["type"] == "welcome"
    return client


def close_code(payload: bytes) -> int:
    return int.from_bytes(payload[:2], "big")


def test_a_second_connection_of_a_device_closes_the_first_with_4409_and_gets_the_next_frame(tmp_path):
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id

    async def main() -> tuple[list[tuple[int, bytes]], bytes, str]:
        async with serving(app) as port, aiohttp.ClientSession() as session:
            first = await raw_session(port, w)
            assert table.send(device, "before-replacement")  # still the first connection's
            second = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await second.send_str(json.dumps(HELLO))
            assert (await second.receive_json())["type"] == "welcome"
            frames, tail = parse_frames(await first.recv_to_end())
            await asyncio.sleep(0.2)  # the replaced connection's own handler has ended by now
            assert table.send(device, "after-replacement")  # the table still holds the second connection
            seen = await second.receive_str()
            await second.send_str(json.dumps(PING))
            assert (await second.receive_json())["type"] == "pong"  # the second keeps working
            await second.close()
            first.close()
            return frames, tail, seen

    frames, tail, seen = run(main())
    assert [op for op, _ in frames] == [TEXT, CLOSE] and tail == b""  # what was queued, then 4409, nothing after
    assert frames[0][1] == b"before-replacement" and close_code(frames[1][1]) == 4409
    assert seen == "after-replacement"


def test_the_replaced_connection_receives_nothing_sent_after_the_replacement(tmp_path):
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id

    async def main() -> list[tuple[int, bytes]]:
        async with serving(app) as port, aiohttp.ClientSession() as session:
            first = await raw_session(port, w)
            second = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await second.send_str(json.dumps(HELLO))
            await second.receive_json()
            for number in range(20):
                assert table.send(device, f"for-the-second-{number}")
            await asyncio.sleep(0.2)
            frames, _ = parse_frames(await first.recv_to_end())
            await second.close()
            first.close()
            return frames

    assert [op for op, _ in run(main())] == [CLOSE]


def test_the_table_has_no_connection_for_an_unknown_or_closed_device(tmp_path):
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id

    async def main() -> None:
        async with serving(app) as port:
            assert not table.send(device, "x") and not await table.close_device(device, 4403)
            client = await raw_session(port, w)
            assert table.send(device, "x") and await table.close_device(device, 4403)
            await client.recv_to_end()
            assert not table.send(device, "x") and not await table.close_device(device, 4403)
            client.close()

    run(main())


def test_close_device_discards_a_queued_burst_and_writes_nothing_after_the_close_frame(tmp_path):
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id

    async def main() -> tuple[list[tuple[int, bytes]], bytes]:
        async with serving(app) as port:
            client = await raw_session(port, w)
            for number in range(50):
                assert table.send(device, f"early-{number}")
            await asyncio.sleep(0.2)  # the writer has sent these
            for number in range(100):
                assert table.send(device, f"burst-{number}")  # queued; the writer has not run yet
            assert await table.close_device(device, 4403)
            frames, tail = parse_frames(await client.recv_to_end())
            client.close()
            return frames, tail

    frames, tail = run(main())
    assert [p.decode() for op, p in frames if op == TEXT] == [f"early-{n}" for n in range(50)]
    assert frames[-1][0] == CLOSE and close_code(frames[-1][1]) == 4403 and tail == b""
    assert [op for op, _ in frames].count(CLOSE) == 1  # no frame of the burst follows it


def test_close_device_aborts_the_transport_500_ms_after_the_close_when_the_peer_does_not_answer(tmp_path):
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id
    chunk = "x" * 60_000

    async def main() -> tuple[float, list[tuple[int, bytes]]]:
        async with serving(app) as port:
            client = await raw_session(port, w, rcvbuf=4096)  # a client that stops reading: the writer stalls
            for number in range(100):
                assert table.send(device, f"{number:03d}{chunk}")
            await asyncio.sleep(0.3)
            started = time.monotonic()
            assert await table.close_device(device, 4403)
            elapsed = time.monotonic() - started
            await asyncio.sleep(0.8)  # still not reading: the server's 500 ms are over
            data = await client.recv_to_end(timeout=3)  # ends: the server's side is gone
            client.close()
            return elapsed, parse_frames(data)[0]

    elapsed, frames = run(main())
    texts = [p for op, p in frames if op == TEXT]
    assert elapsed < 0.7, elapsed  # close_device never waits longer than the abort
    assert 0 < len(texts) < 100 and [t[:3].decode() for t in texts] == [f"{n:03d}" for n in range(len(texts))]
    assert all(
        op != CLOSE for op, _ in frames
    )  # the close frame was still in the server's buffer: the abort dropped it


def test_only_the_writer_task_writes_to_the_socket(tmp_path, monkeypatch):
    w = wiring(tmp_path)
    writers: list[str | None] = []
    original = web.WebSocketResponse.send_str

    async def spy(self: web.WebSocketResponse, data: str, *args: Any, **kwargs: Any) -> None:
        task = asyncio.current_task()
        writers.append(task.get_name() if task else None)
        await original(self, data, *args, **kwargs)

    monkeypatch.setattr(web.WebSocketResponse, "send_str", spy)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_str(json.dumps(HELLO))
        await ws.receive_json()  # welcome
        await ws.send_str("{not json")
        await ws.receive_json()  # error
        await ws.send_str(json.dumps(MSG_IN))
        await ws.send_str(json.dumps(PING))
        await ws.receive_json()  # pong

    async def main() -> None:
        async with serving(app_for(w)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await scenario(ws)
            await ws.close()

    run(main())
    assert len(writers) == 3 and set(writers) == {"glasses-ws-writer"}


def test_shutdown_closes_every_connection_with_1001_without_waiting_for_the_handlers(tmp_path):
    w = wiring(tmp_path)

    async def main() -> tuple[WSMsgType, int | None, float]:
        async with aiohttp.ClientSession() as session:
            async with serving(app_for(w)) as port:
                ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
                await ws.send_str(json.dumps(HELLO))
                await ws.receive_json()
                waiting = asyncio.ensure_future(ws.receive())  # reads (and so answers) the close frame
                started = time.monotonic()
            message = await waiting
            return message.type, ws.close_code, time.monotonic() - started

    kind, code, took = run(main())
    assert (kind, code) == (WSMsgType.CLOSE, 1001) and took < 3  # aiohttp would otherwise wait its 60 s timeout


class StuckWs:
    closed = False

    async def send_str(self, text: str) -> None:
        await asyncio.Event().wait()

    async def close(self, *, code: int) -> bool:
        return True


def test_a_full_send_queue_refuses_the_next_frame_and_close_empties_it():
    async def main() -> tuple[list[bool], int, int, bool]:
        conn = Connection("dev", StuckWs(), None)  # type: ignore[arg-type]
        accepted = [conn.send(str(number)) for number in range(QUEUE_MAX + 1)]  # the writer has not run yet
        queued = conn.pending
        await conn.close(4403)
        return accepted, queued, conn.pending, conn.send("after the close")

    accepted, queued, left, after = run(main())
    assert accepted == [True] * QUEUE_MAX + [False] and (queued, left, after) == (QUEUE_MAX, 0, False)


@pytest.mark.parametrize("how", ["the device hangs up", "close_device"])
def test_the_table_forgets_a_connection_when_it_ends(tmp_path, how):
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id

    async def main() -> None:
        async with serving(app) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await ws.send_str(json.dumps(HELLO))
            await ws.receive_json()
            if how == "close_device":
                await table.close_device(device, 4403)
            else:
                await ws.close()
            await asyncio.sleep(0.2)
            assert not table.send(device, "x")

    run(main())
