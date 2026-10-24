# SPDX-License-Identifier: Apache-2.0
"""``Connection.yield_turn`` / ``reads_paused`` / ``pong`` (T1.07c, T1.184b): the socket is not read during the
turn or while a frame is handled, is read again after it on every exit path, a close lifts the pause, a pong write
that does not finish aborts the connection, and a transport that is closing or absent is never touched."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from glasses import connections
from glasses.connections import Connection

TEST_TIMEOUT_S = 10  # a hung write must fail its test, not the whole run


class FakeTransport(asyncio.Transport):
    """Records what the connection does to the read side."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.paused = False
        self.closing = False

    def is_closing(self) -> bool:
        return self.closing

    def pause_reading(self) -> None:
        self.calls.append("pause")
        self.paused = True

    def resume_reading(self) -> None:
        self.calls.append("resume")
        self.paused = False

    def abort(self) -> None:
        self.calls.append("abort")
        self.closing = True


async def with_connection(transport: asyncio.Transport | None, body: Callable[[Connection], Awaitable[Any]]) -> Any:
    conn = Connection("0" * 16, None, transport)  # type: ignore[arg-type]  # the writer never runs: nothing is sent
    try:
        return await body(conn)
    finally:
        await conn.stop()


def test_the_socket_is_not_read_while_another_task_runs_and_is_read_again_after():
    transport, seen = FakeTransport(), []

    async def body(conn: Connection) -> None:
        async def other() -> None:
            seen.append(transport.paused)  # runs in the turn that yield_turn gives away

        task = asyncio.get_running_loop().create_task(other())
        await conn.yield_turn()
        await task

    asyncio.run(with_connection(transport, body))
    assert seen == [True]
    assert transport.calls == ["pause", "resume"] and not transport.paused


def test_a_cancelled_turn_still_reads_again():
    transport = FakeTransport()

    async def body(conn: Connection) -> None:
        turn = asyncio.get_running_loop().create_task(conn.yield_turn())
        await asyncio.sleep(0)  # the turn has paused and is suspended at its yield
        assert transport.paused
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn

    asyncio.run(with_connection(transport, body))
    assert transport.calls == ["pause", "resume"] and not transport.paused


def test_a_transport_that_closes_during_the_turn_is_not_resumed():
    transport = FakeTransport()

    async def body(conn: Connection) -> None:
        async def other() -> None:
            transport.closing = True  # e.g. the abort timer of a close that started elsewhere

        task = asyncio.get_running_loop().create_task(other())
        await conn.yield_turn()
        await task

    asyncio.run(with_connection(transport, body))
    assert transport.calls == ["pause"]


def test_a_transport_that_is_already_closing_is_left_alone():
    transport = FakeTransport()
    transport.closing = True
    asyncio.run(with_connection(transport, lambda conn: conn.yield_turn()))
    assert transport.calls == []


def test_a_connection_without_a_transport_still_yields_one_turn():
    ran: list[str] = []

    async def body(conn: Connection) -> None:
        asyncio.get_running_loop().call_soon(ran.append, "other")
        await conn.yield_turn()
        assert ran == ["other"]

    asyncio.run(with_connection(None, body))


def test_a_failing_pause_does_not_leave_the_turn_half_done():
    """If the transport refuses to pause, the error reaches the frame loop (the session ends) and nothing is resumed."""

    class Refusing(FakeTransport):
        def pause_reading(self) -> None:
            self.calls.append("pause")
            raise RuntimeError("cannot pause")

    transport = Refusing()

    async def body(conn: Connection) -> None:
        try:
            await conn.yield_turn()
        except RuntimeError:
            return
        raise AssertionError("the error was swallowed")

    asyncio.run(with_connection(transport, body))
    assert transport.calls == ["pause"]


def test_the_pause_holds_for_the_whole_block_and_nested_turns_do_not_resume_it():
    transport, seen = FakeTransport(), []

    async def body(conn: Connection) -> None:
        with conn.reads_paused():
            await conn.yield_turn()  # the frame loop's own turn, inside the held pause
            seen.append(transport.paused)
            await asyncio.sleep(0.01)  # e.g. a slow on_frame
            seen.append(transport.paused)

    asyncio.run(with_connection(transport, body))
    assert seen == [True, True] and transport.calls == ["pause", "resume"]


def test_an_error_in_the_block_still_reads_again():
    transport = FakeTransport()

    async def body(conn: Connection) -> None:
        with pytest.raises(ValueError), conn.reads_paused():
            raise ValueError

    asyncio.run(with_connection(transport, body))
    assert transport.calls == ["pause", "resume"]


class HungWs:
    """A WebSocket whose pong write never finishes (the peer does not read); ``close`` is recorded."""

    def __init__(self) -> None:
        self.cancelled = False
        self.closed: list[int] = []

    async def pong(self, payload: bytes) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self, code: int) -> bool:
        self.closed.append(code)
        return True


def test_a_pong_write_that_does_not_finish_aborts_the_connection_and_returns(monkeypatch):
    monkeypatch.setattr(connections, "WRITE_TIMEOUT_S", 0.05)
    transport, ws = FakeTransport(), HungWs()

    async def body(conn: Connection) -> None:
        conn.ws = ws  # type: ignore[assignment]
        assert await conn.pong(b"x") is False
        assert conn.send("late") is False  # nothing is queued after the abort

    # its own bound: with the pong timeout gone the write waits for ever, and the test must fail, not hang the suite
    asyncio.run(asyncio.wait_for(with_connection(transport, body), TEST_TIMEOUT_S))
    assert ws.cancelled and transport.calls == ["abort"] and ws.closed == []


def test_a_close_lifts_the_pause_so_the_peers_close_frame_is_read(monkeypatch):
    monkeypatch.setattr(connections, "ABORT_AFTER_S", 0.05)
    transport, ws = FakeTransport(), HungWs()

    async def body(conn: Connection) -> None:
        conn.ws = ws  # type: ignore[assignment]
        with conn.reads_paused():
            await conn.close(1008)
            assert transport.calls == ["pause", "resume"]

    asyncio.run(with_connection(transport, body))
    assert ws.closed == [1008] and transport.calls[:2] == ["pause", "resume"]
