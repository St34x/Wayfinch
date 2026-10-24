# SPDX-License-Identifier: Apache-2.0
"""Outbox edges of the T1.249 changes: a failing or concurrent ack leaves one pending value per device and no stale
entry, ``closed`` after a purge changes nothing, more state-directory modes, and a rolled-back delivery logs no cap
line."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import pytest
from outbox_helpers import A, B, Dev, FakeLink, answer, outbox, run, seqs, store

from glasses.outbox import CAP, FILE_NAME, OutboxError

DIRECTORY_TEXT = "its directory must belong to the plugin user, not be group- or world-writable"


def test_a_failing_ack_job_leaves_no_pending_entry_and_the_next_ack_is_a_new_job(tmp_path):
    box = outbox(tmp_path)
    calls: list[tuple[str, int]] = []
    real = box._store.ack

    def flaky(device_id: str, up_to: int) -> None:
        calls.append((device_id, up_to))
        if len(calls) == 1:
            raise OSError("disk full")
        real(device_id, up_to)

    box._store.ack = flaky  # type: ignore[method-assign]

    async def main() -> list[int]:
        await box.hello(A, 0, FakeLink())
        for _ in range(3):
            await box.send(lambda: [Dev(A)], "work", answer())
        with pytest.raises(OSError):
            await asyncio.wrap_future(box.ack_soon(A, 1))
        assert A not in box._acks  # the failed job took its entry with it
        await asyncio.wrap_future(box.ack_soon(A, 2))
        remaining = seqs(tmp_path, A)
        await box.close()
        return remaining

    assert run(main()) == [3] and calls == [(A, 1), (A, 2)]


def test_acks_from_several_threads_at_once_make_one_store_call_with_the_highest_value(tmp_path):
    box = outbox(tmp_path)
    calls: list[tuple[str, int]] = []
    real = box._store.ack
    box._store.ack = lambda d, u: (calls.append((d, u)), real(d, u))[1]  # type: ignore[method-assign]
    stalled, futures = threading.Event(), []

    def push(values: range) -> None:
        futures.extend(box.ack_soon(B, up_to) for up_to in values)

    async def main() -> None:
        box._pool.submit(stalled.wait)  # the worker is busy: every ack queues behind it
        threads = [threading.Thread(target=push, args=(range(start, 400, 4),)) for start in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stalled.set()
        await asyncio.wrap_future(futures[0])
        await box.close()

    run(main())
    assert calls == [(B, 399)] and len({id(f) for f in futures}) == 1


def test_closed_after_a_purge_and_closed_twice_change_nothing(tmp_path):
    box, link = outbox(tmp_path), FakeLink()

    async def main() -> None:
        await box.hello(A, 0, link)
        await box.purge(A)
        assert A not in box._links
        box.closed(A, link)
        box.closed(A, link)
        assert not await box.send(lambda: [Dev(A, revoked_at="2026-10-20T00:00:00Z")], "work", answer())
        await box.close()

    run(main())
    assert link.sent == [] and seqs(tmp_path, A) == []


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o777, 0o1777])
def test_a_state_dir_that_group_or_others_can_write_to_is_refused_without_creating_the_file_or_naming_the_path(
    tmp_path, mode
):
    state = tmp_path / "state-dir-marker-5521"
    state.mkdir()
    state.chmod(mode)
    box = store(state)
    with pytest.raises(OutboxError) as refused:
        box.open()
    box.close()
    assert str(refused.value) == f"{FILE_NAME}: {DIRECTORY_TEXT}" and "marker-5521" not in str(refused.value)
    assert not (state / FILE_NAME).exists()


@pytest.mark.parametrize("mode", [0o710, 0o750, 0o705])
def test_a_state_dir_only_the_owner_can_write_to_is_accepted_whoever_else_may_read_it(tmp_path, mode):
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(mode)
    box = store(state)
    box.open()
    box.close()
    assert (state / FILE_NAME).stat().st_mode & 0o777 == 0o600


def test_a_state_dir_reached_through_a_symlink_is_checked_at_its_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o777)
    link = tmp_path / "state"
    link.symlink_to(target, target_is_directory=True)
    box = store(link)
    with pytest.raises(OutboxError, match=DIRECTORY_TEXT):
        box.open()
    box.close()
    assert not (target / FILE_NAME).exists()


def test_a_rolled_back_delivery_logs_no_cap_line_and_the_next_call_logs_only_its_own(tmp_path, caplog: Any):
    box = store(tmp_path)
    for _ in range(CAP):
        box.add(A, lambda seq: f"frame-{seq}")
    devices: Any = lambda: [Dev(A), Dev(B)]  # noqa: E731 - one expression, read as a value

    def build(device_id: str, seq: int) -> str:
        if device_id == B:
            raise RuntimeError("store failed for B")  # A's add (a cap drop) is already in the transaction
        return f"frame-{seq}"

    with caplog.at_level(logging.INFO, logger="glasses.outbox"):
        with pytest.raises(RuntimeError):
            box.deliver(devices, "work", build)
        assert not [r for r in caplog.records if "over the cap" in r.getMessage()]
        box.deliver(lambda: [Dev(A)], "work", build)
    box.close()
    lines = [r.getMessage() for r in caplog.records if "over the cap" in r.getMessage()]
    # CAP rows + the new one = CAP + 1; two go (room for the overflow row): the rolled-back drop did not happen
    assert lines == [f"glasses outbox: 2 frames over the cap dropped (deviceId={A})"]
    assert (tmp_path / FILE_NAME).exists()
