# SPDX-License-Identifier: Apache-2.0
"""A ``hello`` of a device the signer revoked (T1.248): ``Store.hello`` reads the registry once and purges a revoked
device in the same transaction, replaying nothing; and a tombstone refusal through the production hook
``on_revoked(None)`` that lands while a replay read is in flight makes ``Outbox.hello`` wait for the sweep queued
behind the read, so the revoked device gets no queued frame and no live link."""

from __future__ import annotations

import asyncio
import logging
import threading

from outbox_helpers import A, B, Dev, FakeLink, answer, outbox, rows, run, seqs, store

from glasses.outbox import MAX_SEQ, Outbox

REVOKED = "2026-10-21T00:00:00Z"
PLANTED = "planted-race-answer-4471"


def test_store_hello_of_a_registry_revoked_device_purges_it_and_replays_nothing(tmp_path):
    s = store(tmp_path)
    for text in ("q1", "q2", "q3"):
        s.add(A, lambda seq, text=text: answer(text)(A, seq))
    s.add(B, lambda seq: answer()(B, seq))
    reads = []

    def devices():
        reads.append(1)
        return [Dev(A, revoked_at=REVOKED), Dev(B)]

    assert s.hello(A, 0, devices) == (1, [], MAX_SEQ, True)
    assert len(reads) == 1 and seqs(tmp_path, A) == [] and seqs(tmp_path, B) == [1]
    assert [d for (d, _at) in rows(tmp_path, "purged")] == [A]
    assert s.hello(B, 0, devices)[1] != []  # another device is replayed as before
    assert s.add(A, lambda seq: answer()(A, seq)) == []  # purged for good


def test_outbox_hello_reads_its_registry_and_a_revoked_device_gets_no_frame(tmp_path):
    registry = [Dev(A)]
    box = Outbox(store(tmp_path), [].append, lambda: registry)

    async def main() -> tuple[int, list[str], list]:
        for text in ("q1", "q2", "q3"):
            await box.send(lambda: [Dev(A)], "work", answer(text))
        registry[:] = [Dev(A, revoked_at=REVOKED)]
        link = FakeLink()
        resume, frames = await box.hello(A, 0, link)
        await box.send(lambda: [Dev(A)], "work", answer("late"))  # an earlier registry copy still lists it
        await box.close()
        return resume, frames, link.sent

    resume, frames, live = run(main())
    assert (resume, frames, live) == (1, [], []) and seqs(tmp_path, A) == []


def _gated_hello(box: Outbox) -> tuple[threading.Event, threading.Event]:
    """Hold the worker inside ``Store.hello``, after its registry read, until *release* is set."""
    release, inside = threading.Event(), threading.Event()
    real = box._store.hello

    def slow(device_id: str, acked: int, devices=None):
        found = real(device_id, acked, devices)
        inside.set()
        release.wait(10)
        return found

    box._store.hello = slow  # type: ignore[method-assign]
    return release, inside


def test_a_tombstone_refusal_during_a_replay_in_flight_hands_the_revoked_device_no_queued_text(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    registry = [Dev(A)]
    box = outbox(tmp_path)
    box._devices = lambda: registry  # as connect() wires it: the registry hello checks
    hook = box.on_revoked(lambda: registry)  # the production hook of server_auth.note_revoked

    async def main() -> tuple[list[str], FakeLink]:
        for index in range(3):
            await box.send(lambda: registry, "work", answer(f"{PLANTED}-{index}"))
        release, inside = _gated_hello(box)
        link = FakeLink()
        replay = asyncio.create_task(box.hello(A, 0, link))
        await asyncio.to_thread(inside.wait, 5)  # the read saw A active
        registry[:] = [Dev(A, revoked_at=REVOKED)]  # the signer's revoke lands now
        hook(None)  # a second connection of A is refused 403 through the fingerprint tombstone
        release.set()
        _resume, frames = await replay
        await box.close()
        return frames, link

    frames, link = run(main())
    assert frames == [] and A not in box._links and link.sent == []
    assert seqs(tmp_path, A) == [] and [d for (d, _at) in rows(tmp_path, "purged")] == [A]
    assert PLANTED not in " ".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)


def test_a_tombstone_refusal_for_another_device_during_a_replay_still_replays_this_one(tmp_path):
    registry = [Dev(A), Dev(B)]
    box = outbox(tmp_path)
    box._devices = lambda: registry
    hook = box.on_revoked(lambda: registry)

    async def main() -> list[str]:
        for text in ("q1", "q2", "q3"):
            await box.send(lambda: [Dev(A)], "work", answer(text))
        release, inside = _gated_hello(box)
        replay = asyncio.create_task(box.hello(A, 0, FakeLink()))
        await asyncio.to_thread(inside.wait, 5)
        registry[:] = [Dev(A), Dev(B, revoked_at=REVOKED)]
        hook(None)
        release.set()
        _resume, frames = await replay
        await box.close()
        return frames

    frames = run(main())
    assert len(frames) == 3 and A in box._links and seqs(tmp_path, A) == [1, 2, 3]
