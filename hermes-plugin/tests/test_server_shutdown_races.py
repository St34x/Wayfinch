# SPDX-License-Identifier: Apache-2.0
"""WS server shutdown while an upgrade is still in flight (T1.07c): ``on_shutdown`` closes the connections that
exist at that moment, so an upgrade that completes afterwards must not leave a connection nobody closes."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web
from server_auth_helpers import creds, upgrade_headers
from server_helpers import HELLO, app_for, raw, serving, wiring

from glasses.connections import Connection, ConnectionTable
from glasses.server import _AUTH, build_runner


def run(coro: Awaitable[Any], limit: float = 60) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))  # type: ignore[arg-type]


def test_a_device_whose_upgrade_completes_after_shutdown_began_is_closed_with_1001(tmp_path, monkeypatch):
    w = wiring(tmp_path)
    prepare = web.WebSocketResponse.prepare

    async def slow_prepare(self: web.WebSocketResponse, request: web.BaseRequest) -> Any:
        await asyncio.sleep(0.6)  # authenticated, the 101 not yet sent when shutdown starts (a registry read still
        return await prepare(self, request)  # running then would be answered 503 at once: see the stuck-read test)

    monkeypatch.setattr(web.WebSocketResponse, "prepare", slow_prepare)

    async def main() -> tuple[object, int | None]:
        runner = build_runner(app_for(w))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = int(runner.addresses[0][1])
        async with aiohttp.ClientSession() as session:
            connecting = asyncio.create_task(session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers()))
            await asyncio.sleep(0.2)
            cleanup = asyncio.create_task(runner.cleanup())
            ws = await asyncio.wait_for(connecting, 5)
            await ws.send_str(json.dumps(HELLO))
            kind = await _kind_of_end(ws)
            code = ws.close_code if kind == WSMsgType.CLOSE else None
            await ws.close()  # lets the handler end, so the cleanup (otherwise aiohttp's 60 s wait) finishes
            await asyncio.wait_for(cleanup, 10)
            return kind, code

    assert run(main()) == (WSMsgType.CLOSE, 1001)


async def _kind_of_end(ws: aiohttp.ClientWebSocketResponse) -> object:
    """The type of the first non-text message within 3 s, or ``"still connected"``."""
    try:
        async with asyncio.timeout(3):
            while (message := await ws.receive()).type == WSMsgType.TEXT:
                pass
    except TimeoutError:
        return "still connected"
    return message.type


class Slow:
    """A registry whose read blocks for *seconds* (a registry on a stalled disk)."""

    def __init__(self, seconds: float) -> None:
        self.seconds, self.started = seconds, threading.Event()

    def lookup(self, token: Any, fingerprint: Any) -> tuple[None, bool]:
        self.started.set()
        time.sleep(self.seconds)
        return None, False


def test_shutdown_with_an_upgrade_stuck_in_a_registry_read_returns_within_a_few_seconds(tmp_path):
    w = wiring(tmp_path)
    registry = Slow(6.0)

    async def main() -> float:
        started = 0.0
        async with serving(app_for(w, registry=registry)) as port:
            headers = {**upgrade_headers(), **creds("t" * 43)}
            lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            request = f"GET /ws HTTP/1.1\r\nHost: x\r\n{lines}\r\n"
            asyncio.ensure_future(raw(port, request.encode()))
            while not registry.started.is_set():
                await asyncio.sleep(0.01)
            started = time.monotonic()
        return time.monotonic() - started

    assert run(main()) < 3  # aiohttp waits for the running handler; the hop is only closed afterwards


class RecordingWs:
    """The part of a WebSocket a ``Connection`` uses: remembers the code it was closed with."""

    def __init__(self) -> None:
        self.closed_with: int | None = None

    async def send_str(self, text: str) -> None:
        await asyncio.Event().wait()

    async def close(self, *, code: int) -> bool:
        self.closed_with = code
        return True


def test_close_all_twice_and_an_admit_after_it_leave_the_table_empty_and_close_the_newcomer_with_1001():
    async def main() -> tuple[list[bool], list[int | None], bool]:
        table, early, late = ConnectionTable(), RecordingWs(), RecordingWs()
        admitted = [await table.admit(Connection("a", early, None))]  # type: ignore[arg-type]
        await table.close_all()
        await table.close_all()
        admitted.append(await table.admit(Connection("b", late, None)))  # type: ignore[arg-type]
        return admitted, [early.closed_with, late.closed_with], table.send("b", "x") or table.send("a", "x")

    admitted, codes, delivered = run(main())
    assert admitted == [True, False] and codes == [1001, 1001] and not delivered


def test_an_upgrade_in_a_registry_read_is_answered_503_at_once_when_shutdown_begins(tmp_path):
    w = wiring(tmp_path)
    registry = Slow(6.0)

    async def main() -> tuple[bytes, float]:
        runner = build_runner(app_for(w, registry=registry))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        reader, writer = await asyncio.open_connection("127.0.0.1", int(runner.addresses[0][1]))
        headers = {**upgrade_headers(), **creds("t" * 43)}
        lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        writer.write(f"GET /ws HTTP/1.1\r\nHost: x\r\n{lines}\r\n".encode())
        while not registry.started.is_set():
            await asyncio.sleep(0.01)
        began = time.monotonic()
        cleanup = asyncio.create_task(runner.cleanup())
        reply = await asyncio.wait_for(reader.read(100), 10)
        took = time.monotonic() - began
        writer.close()
        await cleanup
        return reply, took

    reply, took = run(main())
    assert reply.startswith(b"HTTP/1.1 503 ") and took < 3, (reply[:40], took)


def test_on_shutdown_closes_the_registry_hop_already(tmp_path):
    app = app_for(wiring(tmp_path))

    async def main() -> bool:
        app.freeze()
        await app.shutdown()  # on_shutdown only: on_cleanup has not run
        return app[_AUTH].hop._closed

    assert run(main()) is True
