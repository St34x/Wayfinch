# SPDX-License-Identifier: Apache-2.0
"""Bounds of the outbox (T1.09b): one queued ``outbox_overflow`` row per device, ``push`` rows evicted first, the TTL
for every device (also one never written or greeted again, and at ``open``), and the live-write guards: a frame the
replay already carried is not written again, a refused link is not written again, and a refused connection queues
nothing after the refusal."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, cast

from outbox_helpers import A, B, Clock, Dev, FakeLink, answer, outbox, run, seqs, store

from glasses import connections
from glasses.outbox import FILE_NAME, Outbox


def kinds(state, device_id: str) -> list[str]:
    with sqlite3.connect(state / FILE_NAME) as db:
        found = db.execute("SELECT kind FROM rows WHERE device_id=? ORDER BY seq", (device_id,)).fetchall()
    return [kind for (kind,) in found]


def test_600_answers_to_an_offline_device_leave_499_answers_and_one_overflow_row_last(tmp_path):
    s = store(tmp_path)
    for _ in range(600):
        s.add(A, lambda seq: answer()(A, seq))
    held = kinds(tmp_path, A)
    assert len(held) == 500 and held.count("overflow") == 1 and held[-1] == "overflow"
    assert seqs(tmp_path, A)[0] == 102  # 2 answers went for the first overflow row, 1 for each answer after it


def test_over_the_cap_a_queued_push_row_goes_before_older_answers(tmp_path):
    s = store(tmp_path)
    for _ in range(500):
        s.add(A, lambda seq: answer()(A, seq))
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        db.execute("UPDATE rows SET kind='push' WHERE seq=10")  # a planted push row (no producer yet)
    s.add(A, lambda seq: answer()(A, seq))
    kept = seqs(tmp_path, A)
    assert 10 not in kept and 1 not in kept and 2 in kept and len(kept) == 500


def test_the_ttl_drops_rows_of_a_device_that_is_never_written_or_greeted_again(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    s.add(B, lambda seq: answer()(B, seq))
    clock.now += 25 * 3600
    s.add(A, lambda seq: answer()(A, seq))  # a store for another device
    assert seqs(tmp_path, B) == [] and seqs(tmp_path, A) == [1]
    s.add(B, lambda seq: answer()(B, seq))
    clock.now += 25 * 3600
    s.hello(A, 0)  # a hello of another device
    assert seqs(tmp_path, B) == []


def test_open_checks_the_file_and_drops_expired_rows_of_every_device(tmp_path):
    clock = Clock()
    first = store(tmp_path, clock)
    first.add(B, lambda seq: answer()(B, seq))
    first.close()
    clock.now += 25 * 3600
    later = store(tmp_path, clock)
    later.open()
    assert seqs(tmp_path, B) == []


class _Fixed:
    """A store whose ``deliver`` returns a frame the replay already carried (the live/replay race at the worker)."""

    frame = answer()(A, 1)

    def hello(self, _device_id: str, _acked: int, _devices: Any = None) -> tuple[int, list[tuple[int, str]], int, bool]:
        return 1, [(1, self.frame)], 1, False

    def deliver(self, *_args: Any) -> tuple[dict[str, list[tuple[int, str]]], list[str]]:
        return {A: [(1, self.frame)]}, []

    def close(self) -> None:
        pass


def test_a_live_frame_the_replay_already_carried_is_not_written_again():
    box = Outbox(cast(Any, _Fixed()), lambda _id: None, lambda: [])
    link = FakeLink()

    async def main() -> list[str]:
        _resume, replay = await box.hello(A, 0, link)
        await box.send(lambda: [Dev(A)], "work", answer())
        await box.close()
        return replay

    assert len(run(main())) == 1 and link.sent == []  # seq 1 only once: through the replay


def test_a_link_that_refused_a_frame_is_never_written_again(tmp_path):
    box = outbox(tmp_path)
    link = FakeLink(accept=False)

    async def main() -> None:
        await box.hello(A, 0, link)
        for _ in range(3):
            await box.send(lambda: [Dev(A)], "work", answer())
        await box.close()

    run(main())
    assert link.refusals == 1 and seqs(tmp_path, A) == [1, 2, 3]  # the rows stay for the next hello


def test_a_refused_connection_queues_nothing_after_the_refusal():
    async def main() -> tuple[bool, list[str]]:
        class WS:
            written: list[str] = []

            async def send_str(self, text: str) -> None:
                WS.written.append(text)

            async def close(self, code: int) -> bool:
                return True

        conn = connections.Connection(A, WS(), None)  # type: ignore[arg-type]
        conn.refused()
        later = conn.send(json.dumps({"seq": 9}))  # synchronously after the refusal: no later seq after a gap
        await asyncio.sleep(0.05)
        return later, WS.written

    assert run(main()) == (False, [])
