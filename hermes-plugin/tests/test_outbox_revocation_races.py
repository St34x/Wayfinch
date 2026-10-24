# SPDX-License-Identifier: Apache-2.0
"""Revocation against delivery (T1.09b): a revoke that lands while a replay is in flight, recipients are the
non-revoked devices holding the lane, and fifty reconnects with a revoked fingerprint cost a bounded, finishing amount
of work (one sweep each, no task left over, no row, no answer)."""

from __future__ import annotations

import asyncio
import threading
import time

import aiohttp
import pytest
from outbox_helpers import A, B, C, Dev, FakeLink, answer, outbox, rows, run, seqs
from outbox_session_helpers import rig, served

from glasses import outbound

import test_adapter

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument
D = "aaaaaaaaaaaaaaaa"


def _gated_hello(box) -> threading.Event:
    """Hold the worker inside ``Store.hello`` until the returned event is set (a replay read in flight)."""
    release, inside = threading.Event(), threading.Event()
    real = box._store.hello

    def slow(device_id: str, acked: int, devices=None):
        found = real(device_id, acked, devices)
        inside.set()
        release.wait(10)
        return found

    box._store.hello = slow  # type: ignore[method-assign]
    release.inside = inside  # type: ignore[attr-defined]
    return release


def test_a_sweep_queued_behind_a_replay_in_flight_leaves_the_revoked_device_with_no_live_write(tmp_path):
    box = outbox(tmp_path)
    devices = [Dev(A, revoked_at="2026-10-21T00:00:00Z")]

    async def main() -> tuple[bool, list]:
        for text in ("q1", "q2", "q3"):
            await box.send(lambda: [Dev(A)], "work", answer(text))
        release = _gated_hello(box)
        link = FakeLink()
        replay = asyncio.create_task(box.hello(A, 0, link))
        await asyncio.to_thread(release.inside.wait, 5)  # type: ignore[attr-defined]
        revoke = asyncio.create_task(box.sweep(lambda: devices))  # the signer's revoke, noticed by a tombstone
        await asyncio.sleep(0.05)
        release.set()
        await replay
        await revoke
        delivered = await box.send(lambda: [Dev(A)], "work", answer("late"))  # its registry entry still lists it
        await box.close()
        return delivered, link.sent

    delivered, live = run(main())
    assert delivered is False and live == [] and seqs(tmp_path, A) == []


def test_a_purge_by_id_during_a_replay_in_flight_sends_the_revoked_device_no_queued_text(tmp_path):
    box = outbox(tmp_path)
    link = FakeLink()

    async def main() -> tuple[int, list[str], bool]:
        for text in ("q1", "q2", "q3"):
            await box.send(lambda: [Dev(A)], "work", answer(text))
        release = _gated_hello(box)
        replay = asyncio.create_task(box.hello(A, 0, link))
        await asyncio.to_thread(release.inside.wait, 5)  # type: ignore[attr-defined]
        box.purge_soon(A)  # the refusal of a second, revoked connection of the same device
        release.set()
        resume, frames = await replay
        late = await box.send(lambda: [Dev(A)], "work", answer("late"))  # its registry entry still lists it
        await box.close()
        return resume, frames, late

    resume, frames, late = run(main())
    assert frames == []  # the device is revoked: not one queued answer may still be handed to the session
    assert resume == 1 and late is False and link.sent == [] and seqs(tmp_path, A) == []


def test_recipients_are_the_non_revoked_devices_holding_the_lane_and_every_revoked_one_is_purged(tmp_path):
    box = outbox(tmp_path)
    revoked = "2026-10-21T00:00:00Z"
    everyone = [
        Dev(A),
        Dev(B, revoked_at=revoked),
        Dev(C, lanes=("other",)),
        Dev(D, lanes=("other",), revoked_at=revoked),
    ]

    async def main() -> dict[str, FakeLink]:
        links = {device: FakeLink() for device in (A, B, C, D)}
        for device, link in links.items():
            await box.hello(device, 0, link)
        await box.send(lambda: [Dev(B), Dev(D)], "work", answer("old"))  # queued before the revoke: B and D hold rows
        # B and D were welcomed, then revoked; the registry now lists them revoked
        assert await box.send(lambda: everyone, "work", answer()) is True
        await box.close()
        return links

    links = run(main())
    assert [f["seq"] for f in links[A].sent] == [1] and links[C].sent == []
    assert len(links[B].sent) == 1 and len(links[D].sent) == 1  # only the row written live before the revoke
    assert seqs(tmp_path, A) == [1] and seqs(tmp_path, B) == [] and seqs(tmp_path, C) == [] and seqs(tmp_path, D) == []
    assert sorted(device for (device, _at) in rows(tmp_path, "purged")) == sorted([B, D])


def test_fifty_reconnects_with_a_revoked_fingerprint_with_many_revoked_devices_finish_and_leave_nothing(
    adapter_module, tmp_path
):
    r = rig(adapter_module, tmp_path)
    registry = r.w.registry
    for index in range(100):  # a registry that has accumulated revoked entries
        entry, _token = registry.add("old", f"{index + 1:064x}", ["work"])
        registry.revoke(entry.device_id)
    registry.revoke(r.w.device.device_id)
    sweeps = purges = 0
    real_sweep, real_purge = r.box._store.sweep, r.box._store._purge

    def counting_sweep(devices):
        nonlocal sweeps
        sweeps += 1
        return real_sweep(devices)

    def counting_purge(db, device_id):
        nonlocal purges
        purges += 1
        return real_purge(db, device_id)

    r.box._store.sweep, r.box._store._purge = counting_sweep, counting_purge  # type: ignore[method-assign]

    async def main() -> float:
        async with served(r) as port, aiohttp.ClientSession() as session:
            started = time.monotonic()
            for _ in range(50):
                with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                    await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
                assert refused.value.status == 403
            while r.box._sweeper is not None and not r.box._sweeper.done():  # the (coalesced) sweeps end
                await asyncio.sleep(0.05)
            return time.monotonic() - started

    elapsed = run(main())
    assert seqs(tmp_path / "plugin-state", r.w.device.device_id) == [] and elapsed < 20
    assert sweeps <= 50 and purges == 101  # each revoked device is written once: a later sweep skips it
    assert len(rows(tmp_path / "plugin-state", "purged")) == 101


def test_a_send_to_a_lane_only_a_revoked_device_holds_is_a_failed_non_retryable_result_with_the_fixed_text(
    adapter_module, tmp_path
):
    r = rig(adapter_module, tmp_path)
    r.w.registry.revoke(r.w.device.device_id)

    async def main():
        async with served(r):
            return await r.adapter.send("glasses:work", "answer")

    result = run(main())
    assert (result.success, result.retryable, result.error) == (False, False, outbound.NO_RECIPIENT)
    assert seqs(tmp_path / "plugin-state", r.w.device.device_id) == []
