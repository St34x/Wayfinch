# SPDX-License-Identifier: Apache-2.0
"""The frame check's re-reads (T1.248): the stat poll reads a changed file by itself and cuts a device it finds newly
revoked (no frame needed); a read that failed is never taken as read and is retried at most once per ``RETRY_S`` (a
new version at once: ``chmod`` moves the ctime); a read is bounded by ``READ_TIMEOUT_S`` and never queues behind a
hung one; it runs on the check's own worker even while the loop's default executor is full; the key of a version is
taken before its read, so a revoke renamed in during a read is read again; ``close()`` ends the poll."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time

import pytest
from outbox_helpers import Dev
from revoked_ids_helpers import REVOKED, FakeFile, X, Y, settled

from glasses import revoked_ids
from glasses.revoked_ids import ReadPending, RegistryUnread, RevokedIds


async def _cut_times(ids: RevokedIds) -> list[tuple[str, float]]:
    cuts: list[tuple[str, float]] = []

    async def close(device_id: str, code: int) -> bool:
        cuts.append((device_id, time.monotonic()))
        return True

    ids.bind(close, lambda _id: None)
    return cuts


def _revoke_y(f: FakeFile, mode: int = 0o640) -> None:
    f.devices = [Dev(X, revoked_at=REVOKED), Dev(Y, revoked_at=REVOKED)]
    f.write(mode)


def test_the_poll_cuts_an_idle_device_within_a_second_of_its_revoke_without_any_check(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> float:
        await ids.load()
        cuts = await _cut_times(ids)
        await asyncio.sleep(0.3)
        _revoke_y(f)
        written = time.monotonic()
        while not cuts and time.monotonic() - written < 3:
            await asyncio.sleep(0.01)
        ids.close()
        assert [d for d, _ in cuts] == [Y]
        return cuts[0][1] - written

    assert asyncio.run(main()) < 1.0 and f.reads == 2


def test_a_revoke_whose_read_failed_on_its_mode_is_read_at_once_when_chmod_fixes_it(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> bool:
        await ids.load()
        _revoke_y(f, 0o660)  # the signer's revoke, group-writable: the reader refuses it
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        with pytest.raises(RegistryUnread):
            ids.is_revoked(Y)
        os.chmod(f.path, 0o640)  # moves only the ctime: a new version, read at once (no retry wait)
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        return ids.is_revoked(Y)

    assert asyncio.run(main()) is True and f.reads == 3


def test_the_poll_applies_a_chmod_fixed_revoke_within_a_second(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> float:
        await ids.load()
        cuts = await _cut_times(ids)
        _revoke_y(f, 0o660)
        while f.reads < 2:
            await asyncio.sleep(0.01)
        await settled(ids)
        os.chmod(f.path, 0o640)
        fixed = time.monotonic()
        while not cuts and time.monotonic() - fixed < 3:
            await asyncio.sleep(0.01)
        ids.close()
        return cuts[0][1] - fixed

    assert asyncio.run(main()) < 1.0


def test_a_transient_read_error_is_retried_after_retry_s_with_the_file_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(revoked_ids, "RETRY_S", 0.1)
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> bool:
        await ids.load()
        f.fail_once = OSError(24, "Too many open files")
        _revoke_y(f)
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        with pytest.raises(RegistryUnread):  # inside RETRY_S: refused without a read
            ids.is_revoked(Y)
        assert f.reads == 2
        await asyncio.sleep(0.15)
        with pytest.raises(ReadPending):  # the same version, read again
            ids.is_revoked(Y)
        await settled(ids)
        return ids.is_revoked(Y)

    assert asyncio.run(main()) is True and f.reads == 3


def test_a_hundred_checks_on_a_bad_file_cause_at_most_two_reads(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> None:
        await ids.load()
        f.error = True
        _revoke_y(f)
        for _ in range(100):
            with contextlib.suppress(ReadPending, RegistryUnread):
                ids.is_revoked(Y)
            await asyncio.sleep(0.001)

    asyncio.run(main())
    assert 2 <= f.reads <= 3  # the load, then at most two reads of the bad version within about 0.2 s


def test_a_read_over_the_timeout_fails_closed_and_a_hung_worker_gets_no_second_read(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(revoked_ids, "READ_TIMEOUT_S", 0.2)
    monkeypatch.setattr(revoked_ids, "RETRY_S", 0.05)
    caplog.set_level(logging.WARNING, logger="glasses.revoked_ids")
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> tuple[float, bool]:
        await ids.load()
        f.hold = threading.Event()
        _revoke_y(f)
        started = time.monotonic()
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        waited = time.monotonic() - started
        retries = []
        for _ in range(5):  # retries while the worker still hangs: refused at once, nothing queued behind it
            await asyncio.sleep(0.06)
            retry = time.monotonic()
            with pytest.raises((ReadPending, RegistryUnread)):
                ids.is_revoked(Y)
            await settled(ids)
            retries.append(time.monotonic() - retry)
        assert f.reads == 2 and max(retries) < 0.1  # not READ_TIMEOUT_S each: a frame meeting it is not held
        f.hold.set()
        await asyncio.sleep(0.1)
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        ids.close()
        return waited, ids.is_revoked(Y)

    waited, revoked = asyncio.run(main())
    assert 0.15 <= waited < 0.5 and revoked is True and f.reads == 3
    assert [r.getMessage() for r in caplog.records if r.name == "glasses.revoked_ids"] == [
        "glasses revoked ids: registry read failed (TimeoutError); refusing"
    ]


def test_the_read_runs_on_the_checks_own_worker_while_the_default_executor_is_full(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    readers: list[str] = []
    real = f.read

    def named() -> list[Dev]:
        readers.append(threading.current_thread().name)
        return real()

    ids = RevokedIds(f.path, named)
    release = threading.Event()

    async def main() -> float:
        await ids.load()
        loop = asyncio.get_running_loop()
        blocked = [loop.run_in_executor(None, release.wait, 10) for _ in range(40)]  # every default worker busy
        await asyncio.sleep(0.05)
        _revoke_y(f)
        started = time.monotonic()
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        took = time.monotonic() - started
        assert ids.is_revoked(Y) is True
        release.set()
        await asyncio.gather(*blocked)
        return took

    assert asyncio.run(main()) < 0.5
    assert readers and all(name.startswith("glasses-revoked-ids") for name in readers)


def test_a_revoke_renamed_in_while_a_read_is_held_is_read_again(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> bool:
        await ids.load()
        f.hold = threading.Event()
        f.started.clear()
        f.write()  # a change: its read opens this version (Y active) and is held
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await asyncio.to_thread(f.started.wait, 5)
        _revoke_y(f)  # the revoke lands while that read is held
        f.hold.set()
        await settled(ids)
        with pytest.raises(ReadPending):  # the stat was taken before the read: the revoke is a newer version
            ids.is_revoked(Y)
        await settled(ids)
        return ids.is_revoked(Y)

    assert asyncio.run(main()) is True and f.reads == 3


def test_close_ends_the_poll_and_no_read_follows(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> bool:
        await ids.load()
        await _cut_times(ids)
        poll = ids._poll
        ids.close()
        await asyncio.sleep(0)
        _revoke_y(f)
        await asyncio.sleep(1.2)
        return poll is not None and poll.done()

    assert asyncio.run(main()) is True and f.reads == 1
