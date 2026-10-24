# SPDX-License-Identifier: Apache-2.0
"""Purge of a revoked device (T1.09b): nothing of it stays in the file (read through a separate ``sqlite3``
connection) or is ever stored or sent again, at every point the purge runs: ``send`` to a lane it holds, the server's
revocation check (an overlay denial while rows are queued, before and during a session), and ``connect()`` for a
revoke written while the plugin was stopped. T1.08's in-memory dedupe ids of the device go with it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from edge_helpers import short_dir
from outbox_helpers import TS, A, B, Dev, FakeLink, answer, outbox, rows, run, seqs, store
from outbox_session_helpers import device, rig, served, until_pong
from server_helpers import MSG_IN
from signer_registry_helpers import publish

from glasses.outbox import FILE_NAME
from glasses.registry_signer_file import DenyOverlay

import test_adapter
from test_adapter_edge_registry import _extra, _state, as_signer  # noqa: F401 - the fixture is used by name

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument


def drain(box) -> asyncio.Future:  # every call queued before this one has run
    return asyncio.wrap_future(box.ack_soon("ffffffffffffffff", 0))


def test_after_purge_the_file_holds_nothing_of_the_device_and_a_later_send_stores_and_writes_nothing(tmp_path):
    forgotten: list[str] = []
    box = outbox(tmp_path, forgotten=forgotten)
    link_a, link_b = FakeLink(), FakeLink()
    both = lambda: [Dev(A), Dev(B)]  # noqa: E731 - the registry list as a value

    async def main() -> None:
        await box.hello(A, 0, link_a)
        await box.hello(B, 0, link_b)
        for _ in range(3):
            await box.send(both, "work", answer())
        await box.purge(A)
        assert await box.send(both, "work", answer()) is True  # B still gets its row
        assert await box.hello(A, 0, link_a) == (1, [])  # nothing replayed for A, ever
        await box.send(both, "work", answer())
        await box.close()

    run(main())
    assert [r for r in rows(tmp_path) if r[0] == A] == [] and [r for r in rows(tmp_path, "counters") if r[0] == A] == []
    assert [r[0] for r in rows(tmp_path, "purged")] == [A] and seqs(tmp_path, B) == [1, 2, 3, 4, 5]
    assert len(link_a.sent) == 3 and [f["seq"] for f in link_b.sent] == [1, 2, 3, 4, 5] and A in forgotten


def test_purged_text_is_not_left_in_free_pages_of_the_file(tmp_path):
    s = store(tmp_path)
    marker = "only-on-device-a-7712"
    for _ in range(20):
        s.add(A, lambda seq: answer(text=marker)(A, seq))
    s.purge(A)
    s.close()
    assert marker.encode() not in (tmp_path / FILE_NAME).read_bytes()


def test_a_send_purges_a_revoked_recipient_and_stores_for_the_others(tmp_path):
    box = outbox(tmp_path)

    async def main() -> bool:
        await box.send(lambda: [Dev(A), Dev(B)], "work", answer())
        sent = await box.send(lambda: [Dev(A, revoked_at=TS), Dev(B)], "work", answer())
        await box.close()
        return sent

    assert run(main()) is True and seqs(tmp_path, A) == [] and seqs(tmp_path, B) == [1, 2]


def test_a_device_denied_while_3_rows_are_queued_gets_none_of_them_and_its_rows_and_dedupe_ids_go(module, tmp_path):
    r = rig(module, tmp_path)
    device_id = r.w.device.device_id
    frame = dict(MSG_IN, lane="work")

    async def main() -> tuple[int, int | None]:
        async with served(r) as port, aiohttp.ClientSession() as session:
            async with device(port, r, session, 0) as ws:
                await until_pong(ws, r.phone)
                await ws.send_str(json.dumps(frame))  # T1.08 remembers its id
                await until_pong(ws, r.phone)
            for text in ("q1", "q2", "q3"):
                await r.adapter.send("glasses:work", text)
            assert seqs(r.state, device_id) == [1, 2, 3] and r.adapter.router.text.dedupe.senders(MSG_IN["id"])
            r.w.revoked = True  # the deny overlay marks it
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
            await drain(r.box)
            return refused.value.status, None

    status, _ = run(main())
    assert status == 403 and seqs(r.state, device_id) == [] and r.phone.texts() == []
    assert r.adapter.router.text.dedupe.senders(MSG_IN["id"]) == frozenset()
    assert [row for row in rows(r.state, "counters") if row[0] == device_id] == []


def test_a_device_denied_during_its_session_is_closed_4403_and_purged(module, tmp_path):
    r = rig(module, tmp_path)

    async def main() -> int | None:
        async with served(r) as port, aiohttp.ClientSession() as session:
            async with device(port, r, session, 0) as ws:
                await until_pong(ws, r.phone)
                await r.adapter.send("glasses:work", "first")
                r.w.revoked = True
                await ws.send_str(json.dumps(dict(MSG_IN, lane="work")))
                while (await ws.receive(timeout=5)).type not in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    pass
                code = ws.close_code
            late = await r.adapter.send("glasses:work", "late")  # its registry entry still holds the lane
            await drain(r.box)
            assert late.success is False  # the purged device is refused: nothing stored for it again
            return code

    assert run(main()) == 4403 and seqs(r.state, r.w.device.device_id) == []


@pytest.fixture
def module(adapter_module):
    return adapter_module


def test_a_revoke_written_while_the_plugin_was_stopped_leaves_no_row_after_connect(adapter_module, tmp_path, as_signer):  # noqa: F811 - the imported fixtures
    _target, dev, _token = publish(tmp_path)
    state = _state(tmp_path)
    s = store(state)
    for _ in range(3):
        s.add(dev.device_id, lambda seq: answer()(dev.device_id, seq))
    s.close()
    DenyOverlay(state).deny(dev.device_id)  # glasses-admin revoke while the gateway was down
    with short_dir() as d:
        adapter = test_adapter.new_adapter(adapter_module, _extra(Path(d) / "edge.sock", tmp_path))

        async def main() -> None:
            assert await adapter.connect() is True
            await adapter.disconnect()

        run(main())
    assert seqs(state, dev.device_id) == [] and [r[0] for r in rows(state, "purged")] == [dev.device_id]
