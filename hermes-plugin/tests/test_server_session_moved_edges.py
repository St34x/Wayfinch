# SPDX-License-Identifier: Apache-2.0
"""The session moved out of ``server.py`` (T1.249): the names other modules import from ``server`` are still the
session module's own objects, its records keep ``server``'s logger name, and ``on_closed`` runs on every way a
session can end (a raising hook, a replaced connection, the server's shutdown) without ending the server."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from server_helpers import PING, app_for, serving, wiring

from glasses import server, server_session
from glasses.server import CONNECTIONS

from test_server_session import hello

SECRET = "boom-text-3391"


async def _until(done: Any) -> None:
    for _ in range(250):  # up to 5 s
        if done():
            return
        await asyncio.sleep(0.02)


def test_the_names_other_modules_import_from_server_are_the_session_modules_own_objects():
    for name in ("MAX_MSG_SIZE", "CLOSE_REVOKED", "_now", "_ulid", "_frames"):
        assert getattr(server, name) is getattr(server_session, name), name
    assert server.CLOSE_REVOKED == 4403 and server.MAX_MSG_SIZE == 65_536


def test_the_session_logs_under_the_name_of_the_server_not_its_own_module(tmp_path, caplog):
    w = wiring(tmp_path)

    async def main() -> tuple[Any, int | None]:
        async with serving(app_for(w)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await hello(ws)
            w.revoked = True  # the next frame meets the per-frame revocation check
            await ws.send_str(json.dumps(PING))
            message = await ws.receive(timeout=5)
            while message.type == aiohttp.WSMsgType.TEXT:
                message = await ws.receive(timeout=5)
            return message.type, ws.close_code

    with caplog.at_level(logging.INFO):
        kind, close_code = asyncio.run(asyncio.wait_for(main(), 20))
    lines = [r for r in caplog.records if "frame of a revoked device" in r.getMessage()]
    assert kind == aiohttp.WSMsgType.CLOSE and close_code == server_session.CLOSE_REVOKED
    assert [r.name for r in lines] == ["glasses.server"] and w.device.device_id in lines[0].getMessage()


def test_a_raising_on_closed_is_logged_by_type_only_and_the_server_keeps_serving(tmp_path, caplog):
    w = wiring(tmp_path)
    calls: list[str] = []

    def on_closed(device_id: str, conn: Any) -> None:
        calls.append(device_id)
        raise RuntimeError(SECRET)

    async def main() -> list[str]:
        welcomes = []
        async with serving(app_for(w, on_closed=on_closed)) as port, aiohttp.ClientSession() as session:
            for _ in range(2):  # the second session proves the first one's failure left nothing behind
                ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
                welcomes.append((await hello(ws))["type"])
                await ws.close()
                await _until(lambda: len(calls) == len(welcomes))
        return welcomes

    with caplog.at_level(logging.DEBUG):
        welcomes = asyncio.run(asyncio.wait_for(main(), 20))
    assert welcomes == ["welcome", "welcome"] and calls == [w.device.device_id] * 2
    assert "glasses ws: session failed (RuntimeError)" in caplog.text and SECRET not in caplog.text


def test_on_closed_runs_for_a_connection_the_server_closes_on_shutdown(tmp_path):
    w = wiring(tmp_path)
    calls: list[tuple[str, Any]] = []

    async def main() -> None:
        app = app_for(w, on_closed=lambda device_id, conn: calls.append((device_id, conn)))
        async with serving(app) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await hello(ws)
            await app[CONNECTIONS].close_all()  # what on_shutdown does: every connection closed with 1001
            await _until(lambda: calls)
            await ws.close()

    asyncio.run(asyncio.wait_for(main(), 20))
    assert [device_id for device_id, _ in calls] == [w.device.device_id]


def test_a_replaced_connection_reports_its_own_end_and_the_newer_one_reports_only_when_it_ends(tmp_path):
    w = wiring(tmp_path)
    welcomed: list[Any] = []
    ended: list[Any] = []

    async def on_hello(device_id: str, acked: int, conn: Any) -> tuple[int, list[str]]:
        welcomed.append(conn)
        return acked + 1, []

    async def main() -> None:
        app = app_for(w, on_hello=on_hello, on_closed=lambda device_id, conn: ended.append(conn))
        async with serving(app) as port, aiohttp.ClientSession() as session:
            first = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await hello(first)
            second = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await hello(second)  # the newer connection replaced the first (closed 4409)
            await _until(lambda: ended)
            assert ended == [welcomed[0]] and welcomed[1] is not welcomed[0]
            await second.close()
            await _until(lambda: len(ended) == 2)
            await first.close()

    asyncio.run(asyncio.wait_for(main(), 20))
    assert ended == welcomed
