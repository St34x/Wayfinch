# SPDX-License-Identifier: Apache-2.0
"""A change of the signer's ``devices.json`` over the real WS server, the adapter's frame router and outbox, and a real
registry whose writes replace the file by an atomic rename (T1.248): the frame or upgrade that meets the re-read is
held for it, never dropped (the first upgrade after an enrolment, the first ``msg.in`` after a change, a ``hello``),
with no WARNING; a ``hello`` whose check fails is closed ``1013``, never left hanging; the stat poll closes an idle
session of a signer-revoked device ``4403`` with no frame from it, also one that only sends WebSocket pings; 12
revoked of 24 connected devices are all closed and none of their messages is handed on, while every active device's
message is."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from typing import Any

import aiohttp
from aiohttp import WSMsgType
from outbox_session_helpers import hello, revoked_ids, rig, served, until_pong
from server_auth_helpers import creds, new_fingerprints, upgrade_headers
from server_helpers import PING

import test_adapter
from test_inbound_session import msg

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument


def _warnings(caplog: Any) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("glasses")]


async def _closed(ws: aiohttp.ClientWebSocketResponse, limit: float) -> tuple[int | None, int]:
    """The close code (None: still open after *limit* s) and how many text frames came before it."""
    texts, deadline = 0, time.monotonic() + limit
    while (left := deadline - time.monotonic()) > 0:
        try:
            message = await ws.receive(timeout=left)
        except TimeoutError:
            return None, texts
        if message.type is WSMsgType.TEXT:
            texts += 1
        elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            return ws.close_code, texts
    return None, texts


def test_the_first_upgrade_after_an_enrolment_and_the_first_msg_in_after_it_get_through(
    adapter_module, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    r = rig(adapter_module, tmp_path)

    async def main() -> tuple[str, list[str]]:
        async with served(r, revoked_ids(r)) as port, aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{port}/ws"
            ws = await session.ws_connect(url, headers=r.w.headers())
            await ws.send_str(hello(0))
            await until_pong(ws, r.phone)
            phone, token = r.w.registry.add("new phone", new_fingerprints(1)[0], ["work"])  # the enrolment
            await ws.send_str(msg("work", "01JABCDEFGHJKMNPQRSTVWXYZ1"))  # the first msg.in after the change
            headers = {**upgrade_headers(), **creds(token, phone.cert_fingerprint)}
            new_ws = await session.ws_connect(url, headers=headers)  # its very first upgrade: a 503 would raise here
            await new_ws.send_str(hello(0))
            welcome = (await new_ws.receive_json(timeout=5))["type"]
            return welcome, [f["type"] for f in await until_pong(ws, r.phone)]

    welcome, types = asyncio.run(asyncio.wait_for(main(), 20))
    assert welcome == "welcome" and types == ["pong"]
    assert [e.message_id for e in r.adapter.handed_on] == ["01JABCDEFGHJKMNPQRSTVWXYZ1"]
    assert _warnings(caplog) == []  # a routine re-read is no failure


def _held_read(r: Any, gate: threading.Event, fail: dict[str, bool]) -> Any:
    def read() -> Any:
        if gate.is_set():
            time.sleep(0.4)
            if fail["now"]:
                raise OSError(5, "Input/output error")
        return r.w.registry.list()

    return read


def test_a_hello_meeting_the_re_read_gets_welcome_and_one_whose_check_fails_a_clean_1013(adapter_module, tmp_path):
    r = rig(adapter_module, tmp_path)
    slow, fail = threading.Event(), {"now": False}
    ids = revoked_ids(r, _held_read(r, slow, fail))

    async def main() -> tuple[str, float, int | None, int, float]:
        async with served(r, ids) as port, aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{port}/ws"
            slow.set()
            r.w.registry.add("new phone", new_fingerprints(1)[0], ["work"])
            ws = await session.ws_connect(url, headers=r.w.headers())  # the upgrade itself meets the read
            started = time.monotonic()
            await ws.send_str(hello(0))
            first = (await ws.receive_json(timeout=5))["type"]
            welcomed = time.monotonic() - started
            await ws.close()
            fail["now"] = True
            ws = await session.ws_connect(url, headers=r.w.headers())  # meets nothing: the file is unchanged
            r.w.registry.add("third phone", new_fingerprints(2)[1], ["work"])  # its re-read fails
            started = time.monotonic()
            await ws.send_str(hello(0))
            code, texts = await _closed(ws, 5)
            return first, welcomed, code, texts, time.monotonic() - started

    first, welcomed, code, texts, closed = asyncio.run(asyncio.wait_for(main(), 20))
    assert first == "welcome" and welcomed < 1.0
    assert code == 1013 and texts == 0 and closed < 1.5  # never welcomed, never left hanging


def test_an_idle_session_of_a_signer_revoked_device_is_closed_4403_within_1_5_s(adapter_module, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    r = rig(adapter_module, tmp_path)
    device = r.w.device.device_id

    async def main() -> tuple[int | None, int, float]:
        async with served(r, revoked_ids(r)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
            await ws.send_str(hello(0))
            await until_pong(ws, r.phone)
            r.w.registry.revoke(device)  # the device sends nothing from here on
            revoked = time.monotonic()
            code, texts = await _closed(ws, 3)
            return code, texts, time.monotonic() - revoked

    code, texts, elapsed = asyncio.run(asyncio.wait_for(main(), 20))
    assert code == 4403 and texts == 0 and elapsed < 1.5
    assert f"glasses revoked ids: open session of a revoked device closed 4403 (deviceId={device})" in [
        rec.getMessage() for rec in caplog.records
    ]


def test_websocket_pings_do_not_keep_a_signer_revoked_session_open(adapter_module, tmp_path):
    r = rig(adapter_module, tmp_path)

    async def main() -> tuple[int | None, float, int]:
        async with served(r, revoked_ids(r)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers(), autoping=False)
            await ws.send_str(hello(0))
            await until_pong(ws, r.phone)
            r.w.registry.revoke(r.w.device.device_id)
            revoked, pongs = time.monotonic(), [0]

            async def read() -> float:
                async for message in ws:  # ends at the close frame (answered by the client)
                    pongs[0] += message.type is WSMsgType.PONG
                return time.monotonic() - revoked

            reader = asyncio.create_task(read())
            while not reader.done() and time.monotonic() - revoked < 3:
                with contextlib.suppress(aiohttp.ClientConnectionResetError):  # the server is closing it
                    await ws.ping(b"keepalive")  # protocol pings only (10 a second): answered without a check
                await asyncio.sleep(0.1)
            elapsed = await asyncio.wait_for(reader, 3)
            return ws.close_code, elapsed, pongs[0]

    code, elapsed, pongs = asyncio.run(asyncio.wait_for(main(), 20))
    assert code == 4403 and elapsed < 1.5 and pongs >= 1


async def _connect_all(session: aiohttp.ClientSession, url: str, devices: list[tuple[Any, str]]) -> list[Any]:
    sockets = []
    for device, token in devices:
        ws = await session.ws_connect(url, headers={**upgrade_headers(), **creds(token, device.cert_fingerprint)})
        await ws.send_str(hello(0))
        assert (await ws.receive_json(timeout=5))["type"] == "welcome"
        sockets.append(ws)
    return sockets


def test_twelve_of_twenty_four_devices_revoked_one_by_one_are_all_closed_and_none_of_their_messages_handed_on(
    adapter_module, tmp_path
):
    r = rig(adapter_module, tmp_path)
    added = [r.w.registry.add(f"phone {i}", fp, ["work"]) for i, fp in enumerate(new_fingerprints(23))]
    devices = [(r.w.device, r.w.token), *added]
    ids = [f"01JABCDEFGHJKMNPQRSTV{index:05d}" for index in range(24)]

    async def main() -> list[int | None]:
        async with served(r, revoked_ids(r)) as port, aiohttp.ClientSession() as session:
            sockets = await _connect_all(session, f"http://127.0.0.1:{port}/ws", devices)
            for index in range(12):  # 12 separate signer writes; each revoked and one active device speak at once
                r.w.registry.revoke(devices[index][0].device_id)
                await sockets[index].send_str(msg("work", ids[index]))
                await sockets[12 + index].send_str(msg("work", ids[12 + index]))
            codes = [(await _closed(ws, 3))[0] for ws in sockets[:12]]
            for ws in sockets[12:]:
                await ws.send_str(json.dumps(PING))
                while (await ws.receive_json(timeout=5))["type"] != "pong":
                    pass
            return codes + [ws.close_code for ws in sockets[12:]]

    codes = asyncio.run(asyncio.wait_for(main(), 60))
    assert codes == [4403] * 12 + [None] * 12
    assert sorted(e.message_id for e in r.adapter.handed_on) == ids[12:]  # every active one, no revoked one
