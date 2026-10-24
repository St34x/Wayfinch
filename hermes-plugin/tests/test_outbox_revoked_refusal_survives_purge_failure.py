# SPDX-License-Identifier: Apache-2.0
"""A raising ``on_revoked`` callback never changes the refusal of a revoked device: an upgrade by a tombstoned or
revoked device is still ``403 device_revoked`` (one WARNING by type, no text), and a session denied mid-way still
closes ``4403``. Covers the guard in ``server_auth.note_revoked`` for the places the callback is called from."""

from __future__ import annotations

import json

import aiohttp
import pytest
from outbox_helpers import run
from outbox_session_helpers import device, rig, served
from server_helpers import MSG_IN

import test_adapter

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument
SECRET = "purge-failure-secret-must-not-be-logged"
WARNING = "glasses ws: revoked-device hook failed (RuntimeError)"


def _broken(calls: list[str | None]):
    def callback(device_id: str | None) -> None:
        calls.append(device_id)
        raise RuntimeError(SECRET)

    return callback


@pytest.mark.parametrize("branch", ["is_revoked", "tombstone"])
def test_an_upgrade_by_a_revoked_device_is_still_403_when_the_purge_raises(adapter_module, tmp_path, caplog, branch):
    r = rig(adapter_module, tmp_path)
    calls: list[str | None] = []
    if branch == "tombstone":
        r.w.registry.revoke(r.w.device.device_id)  # the signer's revoke: the token is gone, the fingerprint stays
    else:
        r.w.revoked = True  # the deny overlay

    async def main() -> int:
        async with served(r, on_revoked=_broken(calls)) as port, aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
            return refused.value.status

    assert run(main()) == 403
    assert len(calls) == 1 and WARNING in caplog.text and SECRET not in caplog.text


def test_a_session_denied_mid_way_still_closes_4403_when_the_purge_raises(adapter_module, tmp_path, caplog):
    r = rig(adapter_module, tmp_path)
    calls: list[str | None] = []

    async def main() -> int | None:
        async with (
            served(r, on_revoked=_broken(calls)) as port,
            aiohttp.ClientSession() as session,
            device(port, r, session, 0) as ws,
        ):
            await ws.send_str(json.dumps(dict(MSG_IN, lane="work")))
            r.w.revoked = True
            await ws.send_str(json.dumps(dict(MSG_IN, id="01JABCDEFGHJKMNPQRSTVWXYZ1", lane="work")))
            while (await ws.receive(timeout=5)).type not in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                pass
            return ws.close_code

    assert run(main()) == 4403
    assert calls and WARNING in caplog.text and SECRET not in caplog.text
