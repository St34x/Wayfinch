# SPDX-License-Identifier: Apache-2.0
"""Outbox hygiene: the state directory is checked, acks coalesce, the seq-limit WARNING is rate-limited, the cap INFO
line is logged after the commit and a purged revoked device is not purged again on every ``send``."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from outbox_helpers import A, B, Dev, FakeLink, answer, outbox, run, seqs, store

from glasses.outbox import CAP, FILE_NAME, MAX_SEQ, OutboxError

DIRECTORY_TEXT = "its directory must belong to the plugin user, not be group- or world-writable"


@pytest.mark.parametrize(("mode", "accepted"), [(0o700, True), (0o755, True), (0o775, False), (0o757, False)])
def test_an_existing_state_dir_is_accepted_only_when_group_and_others_cannot_write_to_it(tmp_path, mode, accepted):
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(mode)
    box = store(state)
    if accepted:
        box.open()
        assert (state / FILE_NAME).exists()
    else:
        with pytest.raises(OutboxError, match=DIRECTORY_TEXT):
            box.open()
        assert not (state / FILE_NAME).exists()
    box.close()


def test_a_state_dir_of_another_owner_is_refused():
    if os.geteuid() == 0:
        pytest.skip("running as root: no directory of another owner to point at without chown")
    root = Path("/")  # owned by root, not writable by group or others: only the owner check can refuse it
    with pytest.raises(OutboxError, match=DIRECTORY_TEXT):
        store(root).open()


def test_1000_acks_behind_a_stalled_worker_make_one_store_call_and_delete_up_to_the_highest(tmp_path):
    box = outbox(tmp_path)
    calls: list[tuple[str, int]] = []
    real = box._store.ack

    def counted(device_id: str, up_to: int) -> None:
        calls.append((device_id, up_to))
        real(device_id, up_to)

    box._store.ack = counted  # type: ignore[method-assign]

    async def main() -> list[int]:
        await box.hello(A, 0, FakeLink())
        for _ in range(5):
            await box.send(lambda: [Dev(A)], "work", answer())
        stalled = threading.Event()
        box._pool.submit(stalled.wait)  # the worker is busy: every ack queues behind it
        futures = [box.ack_soon(A, up_to) for up_to in [4, *([1, 2, 3] * 333), 2]]
        stalled.set()
        await asyncio.wrap_future(futures[-1])
        remaining = seqs(tmp_path, A)
        await box.close()
        return remaining

    remaining = run(main())
    assert calls == [(A, 4)] and remaining == [5]


def test_acks_of_two_devices_stay_separate_and_a_later_ack_after_the_job_ran_is_a_new_job(tmp_path):
    box = outbox(tmp_path)
    calls: list[tuple[str, int]] = []
    real = box._store.ack
    box._store.ack = lambda d, u: (calls.append((d, u)), real(d, u))[1]  # type: ignore[method-assign]

    async def main() -> None:
        await asyncio.wrap_future(box.ack_soon(A, 1))
        await asyncio.wrap_future(box.ack_soon(A, 2))
        stalled = threading.Event()
        box._pool.submit(stalled.wait)
        pending = [box.ack_soon(A, 3), box.ack_soon(B, 7)]
        stalled.set()
        await asyncio.gather(*(asyncio.wrap_future(f) for f in pending))
        await box.close()

    run(main())
    assert calls == [(A, 1), (A, 2), (A, 3), (B, 7)]


def test_the_seq_limit_warning_is_logged_once_per_device_however_many_replies_follow(tmp_path, caplog):
    box = outbox(tmp_path)

    async def main() -> list[bool]:
        await box.hello(A, MAX_SEQ, FakeLink())  # the device muted itself
        await box.hello(B, MAX_SEQ, FakeLink())
        return [await box.send(lambda: [Dev(A), Dev(B)], "work", answer()) for _ in range(4)]

    with caplog.at_level(logging.WARNING, logger="glasses.outbox"):
        stored = run(main())
    limit = sorted(r.getMessage() for r in caplog.records if "seq limit reached" in r.getMessage())
    expected = sorted(f"glasses outbox: seq limit reached, frames not stored (deviceId={d})" for d in (A, B))
    assert stored == [False] * 4 and limit == expected


def test_the_cap_info_line_is_logged_after_the_commit_not_inside_the_transaction(tmp_path, caplog):
    box = store(tmp_path)
    inside: list[bool] = []

    class Spy(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "over the cap" in record.getMessage():
                inside.append(box._conn.in_transaction)  # type: ignore[union-attr]

    spy = Spy()
    logging.getLogger("glasses.outbox").addHandler(spy)
    try:
        with caplog.at_level(logging.INFO, logger="glasses.outbox"):
            for _ in range(CAP + 1):
                box.add(A, lambda seq: f"frame-{seq}")
    finally:
        logging.getLogger("glasses.outbox").removeHandler(spy)
        box.close()
    assert inside == [False]


def test_a_second_send_after_a_purge_issues_no_purge_statement_for_that_device(tmp_path):
    box = store(tmp_path)
    devices: Any = lambda: [Dev(A, revoked_at="2026-10-20T00:00:00Z"), Dev(B)]  # noqa: E731 - one expression, read as a value
    stored, purged = box.deliver(devices, "work", lambda _device, seq: f"frame-{seq}")
    assert purged == [A] and list(stored) == [B]
    statements: list[str] = []
    box._db().set_trace_callback(statements.append)
    stored, purged = box.deliver(devices, "work", lambda _device, seq: f"frame-{seq}")
    box.close()
    writes = [s for s in statements if s.startswith(("INSERT OR IGNORE INTO purged", "DELETE FROM counters"))]
    assert purged == [A] and [s for s in writes if A in s] == []
    assert not [s for s in statements if s.startswith("DELETE FROM rows") and A in s]
