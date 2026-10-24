# SPDX-License-Identifier: Apache-2.0
"""A device the signer revokes while its session is open (T1.248, the interim check until the live cut): through the
real WS server, the adapter's frame router and outbox, and a real registry whose ``revoke`` replaces ``devices.json``
by an atomic rename, as the signer does. A ``msg.in`` sent after the revoke is never handed on and the session ends
``4403`` within a second; a late ``hello`` replays nothing; another device's revoke leaves a session untouched; no log
record holds a message text."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
from aiohttp import WSMsgType
from outbox_helpers import rows, seqs
from outbox_session_helpers import hello, revoked_ids, rig, served, until_pong
from server_auth_helpers import creds, new_fingerprints, upgrade_headers
from server_helpers import PING

import test_adapter
from test_inbound_session import msg

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument
PLANTED = "planted-secret-answer-7731"
SAID = "planted-said-after-revoke-5512"


def _msg_in(frame_id: str) -> str:
    frame = json.loads(msg("work", frame_id))
    frame["body"]["text"] = SAID
    return json.dumps(frame)


async def _until_close(ws: aiohttp.ClientWebSocketResponse, limit: float = 3.0) -> tuple[list[str], int | None]:
    """Every frame type up to the close, and the close code (None: no close within *limit* seconds)."""
    types: list[str] = []
    deadline = time.monotonic() + limit
    while (left := deadline - time.monotonic()) > 0:
        try:
            message = await ws.receive(timeout=left)
        except TimeoutError:
            break
        if message.type is WSMsgType.TEXT:
            types.append(json.loads(message.data)["type"])
        elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            return types, ws.close_code
    return types, None


def _no_text_logged(caplog: Any) -> None:
    logged = " ".join(f"{record.getMessage()} {record.args!r}" for record in caplog.records)
    assert PLANTED not in logged and SAID not in logged


def test_a_msg_in_sent_after_the_signers_revoke_is_not_handed_on_and_the_session_ends_4403(
    adapter_module, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    r = rig(adapter_module, tmp_path)
    device = r.w.device.device_id

    async def main() -> tuple[list[str], int | None, float]:
        async with served(r, revoked_ids(r)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
            await ws.send_str(hello(0))
            await until_pong(ws, r.phone)
            assert (await r.adapter.send("glasses:work", PLANTED)).success  # a row queued for the device
            await ws.receive_json(timeout=5)  # its live copy
            r.w.registry.revoke(device)  # the signer's revoke: devices.json replaced by an atomic rename
            changed = time.monotonic()
            await ws.send_str(_msg_in("01JABCDEFGHJKMNPQRSTVWXYZ7"))
            types, code = await _until_close(ws)
            return types, code, time.monotonic() - changed

    types, code, elapsed = asyncio.run(asyncio.wait_for(main(), 20))
    assert r.adapter.handed_on == []  # the text said after the revoke never reaches the agent
    assert code == 4403 and elapsed < 1.0 and "msg.out" not in types
    assert seqs(r.state, device) == [] and [d for (d, _at) in rows(r.state, "purged")] == [device]
    _no_text_logged(caplog)


async def _held_hello(r: Any, port: int, session: aiohttp.ClientSession) -> tuple[list[str], int | None]:
    """3 answers stored while the device is offline; it upgrades without ``hello``; the signer revokes it; it sends
    ``hello`` and a ``ping``: every frame type it then sees, and the close code."""
    for index in range(3):
        assert (await r.adapter.send("glasses:work", f"{PLANTED}-{index}")).success
    assert len(seqs(r.state, r.w.device.device_id)) == 3
    ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
    r.w.registry.revoke(r.w.device.device_id)
    await ws.send_str(hello(0))
    await ws.send_str(json.dumps(PING))
    return await _until_close(ws, 1.5)


def test_a_late_hello_after_the_signers_revoke_replays_nothing_when_the_frame_check_passes_it(
    adapter_module, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    r = rig(adapter_module, tmp_path)  # is_revoked: the overlay's place only, so the hello reaches the outbox

    async def main() -> tuple[list[str], int | None]:
        async with served(r) as port, aiohttp.ClientSession() as session:
            return await _held_hello(r, port, session)

    types, _code = asyncio.run(asyncio.wait_for(main(), 20))
    assert types == ["welcome", "pong"]
    assert seqs(r.state, r.w.device.device_id) == [] and rows(r.state) == []
    assert [d for (d, _at) in rows(r.state, "purged")] == [r.w.device.device_id]
    _no_text_logged(caplog)


def test_a_late_hello_after_the_signers_revoke_ends_the_session_4403_with_the_full_wiring(
    adapter_module, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    r = rig(adapter_module, tmp_path)

    async def main() -> tuple[list[str], int | None]:
        async with served(r, revoked_ids(r)) as port, aiohttp.ClientSession() as session:
            return await _held_hello(r, port, session)

    types, code = asyncio.run(asyncio.wait_for(main(), 20))
    assert code == 4403 and "msg.out" not in types and "welcome" not in types
    assert seqs(r.state, r.w.device.device_id) == [] and rows(r.state) == []
    assert [d for (d, _at) in rows(r.state, "purged")] == [r.w.device.device_id]
    _no_text_logged(caplog)


def test_the_signers_revoke_of_another_device_leaves_this_session_its_rows_and_its_msg_in(adapter_module, tmp_path):
    r = rig(adapter_module, tmp_path)
    other, other_token = r.w.registry.add("other", new_fingerprints(1)[0], ["work"])
    ids = revoked_ids(r)

    async def main() -> tuple[int | None, list[str], int | None]:
        async with served(r, ids) as port, aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{port}/ws"
            ws = await session.ws_connect(url, headers=r.w.headers())
            await ws.send_str(hello(0))
            await until_pong(ws, r.phone)
            headers = {**upgrade_headers(), **creds(other_token, other.cert_fingerprint)}
            other_ws = await session.ws_connect(url, headers=headers)
            await other_ws.send_str(hello(0))
            assert (await other_ws.receive_json(timeout=5))["type"] == "welcome"
            assert (await r.adapter.send("glasses:work", PLANTED)).success  # one row for each device
            await ws.receive_json(timeout=5)
            r.w.registry.revoke(other.device_id)
            await ws.send_str(_msg_in("01JABCDEFGHJKMNPQRSTVWXYZ8"))  # may meet the re-read: held, then handed on
            _types, other_code = await _until_close(other_ws)
            while ids._pending is not None:
                await asyncio.sleep(0.01)
            await ws.send_str(_msg_in("01JABCDEFGHJKMNPQRSTVWXYZ9"))
            frames = await until_pong(ws, r.phone)
            return other_code, [f["type"] for f in frames], ws.close_code

    other_code, types, code = asyncio.run(asyncio.wait_for(main(), 20))
    assert other_code == 4403 and types == ["pong"] and code is None
    handed_on = [e.message_id for e in r.adapter.handed_on]
    assert handed_on[-2:] == ["01JABCDEFGHJKMNPQRSTVWXYZ8", "01JABCDEFGHJKMNPQRSTVWXYZ9"]
    assert len(seqs(r.state, r.w.device.device_id)) == 1 and seqs(r.state, other.device_id) == []
