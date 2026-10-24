# SPDX-License-Identifier: Apache-2.0
"""Tombstone sweeps are coalesced (T1.09b): a flood of refused revoked requests runs one sweep plus at most one more
that reads the registry after the last refusal, never one sweep per request; a sweep skips devices already purged."""

from __future__ import annotations

import asyncio
import threading

from outbox_helpers import A, B, Dev, outbox, rows, run, store

REVOKED = "2026-10-21T00:00:00Z"


def test_twenty_tombstone_refusals_during_a_sweep_run_one_more_sweep_and_a_later_one_starts_afresh(tmp_path):
    box = outbox(tmp_path)
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []
    real = box._store.sweep

    def held_sweep(devices):
        calls.append(1)
        started.set()
        release.wait(10)
        return real(devices)

    box._store.sweep = held_sweep  # type: ignore[method-assign]
    hook = box.on_revoked(lambda: [Dev(A, revoked_at=REVOKED)])

    async def idle() -> None:
        while box._sweeper is not None and not box._sweeper.done():
            await asyncio.sleep(0.01)

    async def main() -> tuple[int, int, int]:
        hook(None)
        await asyncio.to_thread(started.wait, 5)
        for _ in range(20):  # refused while the first sweep holds the worker
            hook(None)
        await asyncio.sleep(0.05)
        during = len(calls)
        release.set()
        await idle()
        flood = len(calls)
        hook(None)  # a refusal after the sweeps ended
        await idle()
        await box.close()
        return during, flood, len(calls)

    during, flood, after = run(main())
    assert (during, flood, after) == (1, 2, 3)
    assert [device for (device, _at) in rows(tmp_path, "purged")] == [A]


def test_a_sweep_writes_nothing_for_a_device_already_purged(tmp_path):
    box = store(tmp_path)
    devices = [Dev(A, revoked_at=REVOKED), Dev(B, revoked_at=REVOKED)]
    assert box.sweep(lambda: devices[:1]) == [A]
    purges: list[str] = []
    real = box._purge

    def counting(db, device_id: str) -> None:
        purges.append(device_id)
        real(db, device_id)

    box._purge = counting  # type: ignore[method-assign]
    assert box.sweep(lambda: devices) == [A, B]  # both are dropped on the loop side, only B is written
    box.close()
    assert purges == [B] and sorted(device for (device, _at) in rows(tmp_path, "purged")) == sorted([A, B])
