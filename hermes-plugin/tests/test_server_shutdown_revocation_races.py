# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07c), shutdown racing revocation, a late upgrade and a stalled gate: a connection gets one close frame
whoever asks first, a device that is admitted after shutdown began is never announced or served, and shutdown waits for
a stalled gate no longer than the gate's own bound."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web
from server_auth_helpers import creds, upgrade_headers
from server_helpers import HELLO, PING, app_for, wiring
from server_raw_ws import CLOSE, TEXT, RawWs, frame_bytes, say_hello

from glasses import server_auth
from glasses.connections import Connection, ConnectionTable
from glasses.server import CONNECTIONS, build_runner


def run(coro: Any, limit: float = 60) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))


class HeldClose:
    """A WebSocket whose close frame is held until released: its connection is mid-close for a while."""

    def __init__(self) -> None:
        self.codes: list[int] = []
        self.release = asyncio.Event()

    async def send_str(self, text: str) -> None:
        await asyncio.Event().wait()

    async def close(self, *, code: int) -> bool:
        self.codes.append(code)
        await self.release.wait()
        return True


def test_revoking_a_device_while_shutdown_is_closing_its_connection_closes_it_once_and_reports_none():
    async def main() -> tuple[bool, list[int]]:
        table, ws = ConnectionTable(), HeldClose()
        await table.admit(Connection("a", ws, None))  # type: ignore[arg-type]
        closing = asyncio.create_task(table.close_all())
        await asyncio.sleep(0)
        revoked = await table.close_device("a", 4403)  # the revocation push arrives mid-shutdown
        ws.release.set()
        await closing
        return revoked, ws.codes

    assert run(main()) == (False, [1001])


def test_a_shutdown_that_begins_while_a_revocation_close_is_in_progress_does_not_close_the_connection_again():
    async def main() -> tuple[bool, list[int], bool]:
        table, ws = ConnectionTable(), HeldClose()
        await table.admit(Connection("a", ws, None))  # type: ignore[arg-type]
        revoking = asyncio.create_task(table.close_device("a", 4403))
        await asyncio.sleep(0)
        await table.close_all()  # finds nothing: the connection is already leaving
        ws.release.set()
        return await revoking, ws.codes, table.closed

    assert run(main()) == (True, [4403], True)


def test_a_connection_closed_by_its_own_session_is_not_closed_a_second_time_by_the_shutdown():
    """The frame loop closes a revoked device's connection (4403) while it is still in the table; ``close_all`` then
    finds it there and must not send a second close frame."""

    async def main() -> list[int]:
        table, ws = ConnectionTable(), HeldClose()
        conn = Connection("a", ws, None)  # type: ignore[arg-type]
        await table.admit(conn)
        revoking = asyncio.create_task(conn.close(4403))
        await asyncio.sleep(0)
        ws.release.set()
        await table.close_all()
        await revoking
        return ws.codes

    assert run(main()) == [4403]


def test_a_device_revoked_while_the_server_shuts_down_gets_exactly_one_close_frame(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    w = wiring(tmp_path)
    ready, go = threading.Event(), threading.Event()

    def client(port: int) -> list[int]:
        ws = RawWs(port, w.headers(), timeout=10)
        codes: list[int] = []
        try:
            say_hello(ws)
            ready.set()
            go.wait(10)
            with contextlib.suppress(OSError):
                ws.send(frame_bytes(TEXT, json.dumps(PING).encode()))
            while (got := ws.recv()) is not None:
                if got[0] == CLOSE:
                    codes.append(int.from_bytes(got[1][:2], "big"))
        finally:
            ws.shut()
        return codes

    async def main() -> tuple[list[int], float]:
        runner = build_runner(app_for(w))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        reading = asyncio.get_running_loop().run_in_executor(None, client, int(runner.addresses[0][1]))
        while not ready.is_set():
            await asyncio.sleep(0.01)
        w.revoked = True
        go.set()
        began = time.monotonic()
        await runner.cleanup()
        return await reading, time.monotonic() - began

    codes, took = run(main())
    assert len(codes) == 1 and codes[0] in (4403, 1001), codes  # whichever came first, only one close frame
    assert took < 5 and w.frames == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_device_admitted_after_shutdown_began_is_neither_announced_nor_served(tmp_path, monkeypatch):
    w = wiring(tmp_path)
    announced: list[str] = []
    prepare = web.WebSocketResponse.prepare

    async def slow_prepare(self: web.WebSocketResponse, request: web.BaseRequest) -> Any:
        await asyncio.sleep(0.6)
        return await prepare(self, request)

    monkeypatch.setattr(web.WebSocketResponse, "prepare", slow_prepare)

    async def main() -> tuple[object, bool]:
        app = app_for(w, on_authenticated=lambda device: announced.append(device.device_id))
        runner = build_runner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = int(runner.addresses[0][1])
        async with aiohttp.ClientSession() as session:
            connecting = asyncio.create_task(session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers()))
            await asyncio.sleep(0.2)
            cleanup = asyncio.create_task(runner.cleanup())
            ws = await asyncio.wait_for(connecting, 5)
            await ws.send_str(json.dumps(HELLO))
            async with asyncio.timeout(5):
                while (message := await ws.receive()).type == WSMsgType.TEXT:
                    pass
            await ws.close()
            await asyncio.wait_for(cleanup, 10)
        return message.type, app[CONNECTIONS].send(w.device.device_id, "x")

    assert run(main()) == (WSMsgType.CLOSE, False)
    assert announced == [] and w.frames == []


def test_shutdown_with_an_upgrade_stuck_in_the_gate_ends_with_the_gates_own_bound_not_after_60_seconds(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server_auth, "GATE_TIMEOUT", 1.0)  # the constant is 5 s; the shape of the bound is the same
    w = wiring(tmp_path)

    async def main() -> tuple[bytes, float]:
        entered = asyncio.Event()

        async def never() -> bool:
            entered.set()
            await asyncio.Event().wait()
            return True

        runner = build_runner(app_for(w, gate_ok_async=never))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        reader, writer = await asyncio.open_connection("127.0.0.1", int(runner.addresses[0][1]))
        lines = "".join(f"{k}: {v}\r\n" for k, v in {**upgrade_headers(), **creds("t" * 43)}.items())
        writer.write(f"GET /ws HTTP/1.1\r\nHost: x\r\n{lines}\r\n".encode())
        await entered.wait()
        began = time.monotonic()
        cleanup = asyncio.create_task(runner.cleanup())
        reply = await asyncio.wait_for(reader.read(100), 10)
        took = time.monotonic() - began
        writer.close()
        await cleanup
        return reply, took

    reply, took = run(main())
    assert reply.startswith(b"HTTP/1.1 503 ") and took < 4, (reply[:40], took)
