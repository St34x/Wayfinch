# SPDX-License-Identifier: Apache-2.0
"""WS server connection table under races (T1.07c): several upgrades of one device at once, a replacement racing
``close_device``, stalled peers on ``close_device`` / ``close_all`` / shutdown, and a writer that fails."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Awaitable
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from server_helpers import HELLO, PING, Wiring, app_for, serving, wiring
from ws_raw_helpers import CLOSE, TEXT, RawClient, parse_frames

from glasses.connections import Connection, ConnectionTable
from glasses.server import CONNECTIONS


def run(coro: Awaitable[Any], limit: float = 60) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))  # type: ignore[arg-type]


async def stalled_session(port: int, w: Wiring) -> RawClient:
    """A raw client past ``hello`` that stops reading (a tiny receive buffer): the server's writer stalls."""
    client = await RawClient.connect(port, w.headers(), rcvbuf=4096)
    await client.send_json(HELLO)
    assert json.loads((await client.recv_frames(1))[0][1])["type"] == "welcome"
    return client


async def hello_and_wait(session: aiohttp.ClientSession, port: int, w: Wiring, jitter: float) -> tuple[Any, ...]:
    """One device client that connects after *jitter* seconds, says hello and reports how its socket ended."""
    await asyncio.sleep(jitter)
    ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
    await ws.send_str(json.dumps(HELLO))
    seen: list[Any] = []
    try:
        async with asyncio.timeout(1.5):
            await _read_until_closed(ws, seen)
    except TimeoutError:
        return ("open", None, seen, ws)
    return ("closed", ws.close_code, seen, ws)


async def _read_until_closed(ws: aiohttp.ClientWebSocketResponse, seen: list[Any]) -> None:
    while (message := await ws.receive()).type == WSMsgType.TEXT:
        seen.append(json.loads(message.data)["type"])


@pytest.mark.parametrize("clients", [2, 6])
def test_upgrades_of_one_device_at_once_leave_exactly_one_connection_and_close_the_rest_with_4409(
    tmp_path, caplog, clients
):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id

    async def main() -> tuple[list[tuple[Any, ...]], list[bool], str | None]:
        async with serving(app) as port, aiohttp.ClientSession() as session:
            outcomes = await asyncio.gather(*(hello_and_wait(session, port, w, 0.0) for _ in range(clients)))
            survivors = [o for o in outcomes if o[0] == "open"]
            sent = table.send(device, "for-the-survivor")
            got = None
            if survivors:
                async with asyncio.timeout(2):
                    got = await survivors[0][3].receive_str()
            late = [table.send(device, "x") for _ in range(2)]  # one connection is registered, whoever won
            return outcomes, [sent, *late], got

    outcomes, sent, got = run(main())
    assert [o[0] for o in outcomes].count("open") == 1 and all(o[1] == 4409 for o in outcomes if o[0] == "closed")
    assert sent == [True, True, True] and got == "for-the-survivor"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text


def test_a_replacement_racing_close_device_leaves_a_consistent_table_every_time(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id
    delays = itertools.cycle((0.0, 0.004, 0.011, 0.02, 0.03, 0.007, 0.015, 0.025))  # spread over the race window

    async def round_(port: int, session: aiohttp.ClientSession) -> tuple[bool, bool]:
        first = await hello_and_wait(session, port, w, 0.0)
        assert first[0] == "open"
        newcomer = asyncio.create_task(hello_and_wait(session, port, w, next(delays)))
        await asyncio.sleep(next(delays))
        closed = await table.close_device(device, 4403)
        replacement = await newcomer
        await asyncio.sleep(0.7)  # every close and abort has had its time
        alive = table.send(device, "probe")
        if replacement[0] == "open":
            async with asyncio.timeout(2):
                assert await replacement[3].receive_str() == "probe" if alive else True
            await replacement[3].close()
        await first[3].close()
        await table.close_device(device, 1000)
        return closed, alive

    async def main() -> list[tuple[bool, bool]]:
        async with serving(app) as port, aiohttp.ClientSession() as session:
            return [await round_(port, session) for _ in range(8)]

    results = run(main())
    assert all(closed for closed, _ in results)  # the first connection was always there to close
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text
    assert not table.send(device, "after")


def test_a_replacement_of_a_peer_that_never_answers_is_welcomed_within_the_abort_time(tmp_path):
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id
    chunk = "x" * 60_000

    async def main() -> tuple[float, bytes]:
        async with serving(app) as port, aiohttp.ClientSession() as session:
            old = await stalled_session(port, w)
            for number in range(100):
                assert table.send(device, f"{number:03d}{chunk}")
            await asyncio.sleep(0.3)
            started = time.monotonic()
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await ws.send_str(json.dumps(HELLO))
            kind = (await ws.receive_json())["type"]
            welcomed = time.monotonic() - started
            await ws.close()
            rest = await old.recv_to_end(timeout=3)  # ends: the server's side of the old connection is gone
            old.close()
            return welcomed if kind == "welcome" else -1.0, rest

    welcomed, _ = run(main())
    assert 0 < welcomed < 1.3  # one 500 ms abort of the old transport, never the 10 s of a close handshake


class Stuck:
    """A websocket whose writes never finish and whose close never answers."""

    closed = False

    async def send_str(self, text: str) -> None:
        await asyncio.Event().wait()

    async def close(self, *, code: int) -> bool:
        await asyncio.Event().wait()
        return True


class Aborts:
    def __init__(self) -> None:
        self.aborted = 0

    def abort(self) -> None:
        self.aborted += 1


def test_close_all_over_many_stalled_connections_aborts_them_together_in_about_one_abort_time():
    async def main() -> tuple[float, list[int]]:
        table, transports = ConnectionTable(), []
        for number in range(30):
            transport = Aborts()
            transports.append(transport)
            conn = Connection(f"dev{number}", Stuck(), transport)  # type: ignore[arg-type]
            await table.admit(conn)
            conn.send("queued")
        started = time.monotonic()
        await table.close_all()
        return time.monotonic() - started, [t.aborted for t in transports]

    took, aborted = run(main())
    assert took < 0.9 and aborted == [1] * 30  # concurrent: not 30 x 0.5 s; every transport aborted exactly once


def test_close_twice_aborts_once_and_the_second_call_returns_when_the_first_has_ended():
    async def main() -> tuple[int, float, bool]:
        transport = Aborts()
        conn = Connection("dev", Stuck(), transport)  # type: ignore[arg-type]
        first = asyncio.create_task(conn.close(4403))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await conn.close(4409)  # a second closer (the frame loop after the revocation cut) never returns earlier
        second, first_done = time.monotonic() - started, first.done()
        await first
        await asyncio.sleep(0.1)
        return transport.aborted, second, first_done

    aborted, second, first_done = run(main())
    assert aborted == 1 and first_done and second < 0.6  # bounded by the first close's abort time


def test_shutdown_with_a_stalled_peer_ends_within_the_abort_time_not_aiohttps_60_seconds(tmp_path):
    w = wiring(tmp_path)
    app = app_for(w)
    chunk = "x" * 60_000

    async def main() -> float:
        started = 0.0
        peer = None
        try:
            async with serving(app) as port:
                peer = await stalled_session(port, w)
                for number in range(100):
                    assert app[CONNECTIONS].send(w.device.device_id, f"{number:03d}{chunk}")
                await asyncio.sleep(0.3)
                started = time.monotonic()
        finally:
            took = time.monotonic() - started
            if peer is not None:
                peer.close()
        return took

    assert run(main()) < 3


class Exploding:
    """send_str raises *error* on its first call; later calls would work."""

    closed = False

    def __init__(self, error: BaseException) -> None:
        self.error, self.sent, self.closed_with = error, [], None

    async def send_str(self, text: str) -> None:
        if not self.sent:
            self.sent.append(text)
            raise self.error
        self.sent.append(text)

    async def close(self, *, code: int) -> bool:
        self.closed_with = code
        return True


@pytest.mark.parametrize("error", [RuntimeError("x"), ConnectionResetError(), TypeError("bad frame")])
def test_a_writer_that_fails_ends_quietly_refuses_later_frames_and_close_still_works(error):
    problems: list[dict[str, Any]] = []

    async def main() -> tuple[list[bool], Exploding]:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: problems.append(context))
        ws = Exploding(error)
        conn = Connection("dev", ws, None)  # type: ignore[arg-type]
        assert conn.send("one")
        await asyncio.sleep(0.05)
        after = [conn.send("two"), conn.pending == 0]
        await conn.close(1011)
        await asyncio.sleep(0.05)
        import gc

        gc.collect()  # a task exception nobody retrieved is reported when the task is collected
        return after, ws

    after, ws = run(main())
    assert after == [False, True] and ws.sent == ["one"] and ws.closed_with == 1011
    assert problems == []


@pytest.mark.xfail(
    strict=True, reason="known gap: a writer that dies of a non-socket error leaves the connection open (T1.09)"
)
def test_a_session_whose_writer_died_does_not_stay_open_as_a_connection_that_hears_nothing(tmp_path, monkeypatch):
    w = wiring(tmp_path)
    original = web.WebSocketResponse.send_str
    calls: list[int] = []

    async def failing(self: web.WebSocketResponse, data: str, *args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("writer failure unrelated to the socket")
        await original(self, data, *args, **kwargs)

    monkeypatch.setattr(web.WebSocketResponse, "send_str", failing)

    async def main() -> str:
        async with serving(app_for(w)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await ws.send_str(json.dumps(HELLO))
            assert (await ws.receive_json())["type"] == "welcome"
            await ws.send_str(json.dumps(PING))  # its pong hits the failing write
            try:
                async with asyncio.timeout(2):
                    message = await ws.receive()
            except TimeoutError:
                return "still open and silent"
            return "closed" if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED) else "answered"

    assert run(main()) == "closed"  # now: "still open and silent" (sends return False, the peer is never told)


def frames_of(data: bytes) -> list[int]:
    return [op for op, _ in parse_frames(data)[0]]


def test_close_device_while_the_peer_is_reading_sends_text_then_one_close_frame_and_nothing_after(tmp_path):
    w = wiring(tmp_path)
    app = app_for(w)
    table, device = app[CONNECTIONS], w.device.device_id

    async def main() -> list[int]:
        async with serving(app) as port:
            client = await RawClient.connect(port, w.headers())
            await client.send_json(HELLO)
            await client.recv_frames(1)
            for number in range(20):
                assert table.send(device, f"m{number}")
            closing = asyncio.create_task(table.close_device(device, 4403))
            await asyncio.sleep(0)  # the task has started: the connection is out of the table
            assert not table.send(device, "late")  # the table dropped it at once: nothing is queued for a closing one
            await closing
            ops = frames_of(await client.recv_to_end())
            client.close()
            return ops

    ops = run(main())
    assert ops.count(CLOSE) == 1 and ops[-1] == CLOSE and set(ops) <= {TEXT, CLOSE}
