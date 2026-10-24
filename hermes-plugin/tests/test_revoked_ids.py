# SPDX-License-Identifier: Apache-2.0
"""The stat-keyed revoked-id set of the frame check (T1.248): one registry read per version of the file
(``(st_mtime_ns, st_ctime_ns, st_size, st_ino)``), never one per frame; the read on the check's own worker, off the
loop, the outbox worker and the default executor; a check during a re-read raises ``ReadPending`` (a device already
known as revoked stays revoked); a failed read is never taken as the version read: checks refuse until it is read;
a new inode with the same mtime and size is a change.

Retries, the read timeout and the poll: ``test_revoked_ids_retry.py``; the frame loop over a re-read:
``test_revoked_reread_window.py``."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

import aiohttp
import pytest
from outbox_helpers import Dev
from outbox_session_helpers import hello, revoked_ids, rig, served, until_pong
from revoked_ids_helpers import REVOKED, FakeFile, X, Y, settled
from server_auth_helpers import new_fingerprints

from glasses.revoked_ids import ReadPending, RegistryUnread, RevokedIds

import test_adapter
from test_inbound_session import TEXT, msg

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument


def test_a_device_in_the_last_set_is_revoked_and_another_is_not_after_one_read(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> list[bool]:
        await ids.load()
        return [ids.is_revoked(X) for _ in range(100)] + [ids.is_revoked(Y) for _ in range(100)]

    answers = asyncio.run(main())
    assert answers == [True] * 100 + [False] * 100 and f.reads == 1


def test_a_changed_file_is_read_again_once_and_checks_meanwhile_raise_pending_except_known_revoked(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> tuple[bool, bool]:
        await ids.load()
        f.devices = [Dev(X, revoked_at=REVOKED), Dev(Y, revoked_at=REVOKED)]
        f.write()
        with pytest.raises(ReadPending) as pending:
            ids.is_revoked(Y)  # its revoke may be in the file being read: not answered from the old set
        assert pending.value.read is ids._pending  # the caller can wait for exactly this read
        known = ids.is_revoked(X)  # already revoked: stays revoked during the re-read
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        return known, ids.is_revoked(Y)

    assert asyncio.run(main()) == (True, True) and f.reads == 2


def test_a_replacement_with_the_same_mtime_and_size_but_a_new_inode_is_read_again(tmp_path):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def main() -> bool:
        await ids.load()
        before = os.stat(f.path)
        f.devices = [Dev(X), Dev(Y, revoked_at=REVOKED)]  # same length of ids: the same size
        tmp = f.path.with_suffix(".tmp")
        tmp.write_bytes(f.path.read_bytes())
        os.chmod(tmp, 0o640)
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(tmp, f.path)
        after = os.stat(f.path)
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
        assert after.st_ino != before.st_ino
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        return ids.is_revoked(Y)

    assert asyncio.run(main()) is True and f.reads == 2


def test_a_failed_read_refuses_until_a_read_succeeds_and_is_logged_by_type_once_per_episode(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="glasses.revoked_ids")
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)

    async def change_and_settle() -> None:
        f.write()
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)

    async def main() -> list[tuple[bool, str]]:
        await ids.load()
        f.error = True
        seen = []
        for _ in range(3):  # three changes, every read fails: one episode
            await change_and_settle()
            with pytest.raises(RegistryUnread) as refused:  # never answered from the set read before the change
                ids.is_revoked(Y)
            seen.append((ids.is_revoked(X), type(refused.value).__name__))
        f.error = False
        await change_and_settle()
        assert ids.is_revoked(Y) is False  # read again: answered
        f.error = True
        await change_and_settle()  # a new episode
        return seen

    assert asyncio.run(main()) == [(True, "RegistryUnread")] * 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "glasses revoked ids: registry read failed (RegistryCorruptError); refusing"
    ] * 2


def test_a_device_a_read_finds_newly_revoked_is_purged_then_closed_4403_once_with_one_info_line(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="glasses.revoked_ids")
    f = FakeFile(tmp_path / "devices.json")
    calls: list[tuple[str, Any]] = []

    async def close(device_id: str, code: int) -> bool:
        calls.append(("close", (device_id, code)))
        return True

    ids = RevokedIds(f.path, f.read)

    async def main() -> None:
        await ids.load()  # X is revoked already: nothing to cut at start (not bound yet)
        ids.bind(close, lambda device_id: calls.append(("purge", device_id)))
        f.devices = [Dev(X, revoked_at=REVOKED), Dev(Y, revoked_at=REVOKED)]
        f.write()
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        await asyncio.sleep(0.05)
        f.write()  # a later change with nothing new: no second cut
        with pytest.raises(ReadPending):
            ids.is_revoked(Y + "0")
        await settled(ids)
        await asyncio.sleep(0.05)
        ids.close()

    asyncio.run(main())
    assert calls == [("purge", Y), ("close", (Y, 4403))]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos == [f"glasses revoked ids: open session of a revoked device closed 4403 (deviceId={Y})"]


def test_unchanged_registry_one_read_for_a_hundred_msg_in_frames(adapter_module, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    r = rig(adapter_module, tmp_path)
    reads: list[str] = []

    def counted() -> Any:
        reads.append(threading.current_thread().name)
        return r.w.registry.list()

    async def main() -> list[dict[str, Any]]:
        async with served(r, revoked_ids(r, counted)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
            await ws.send_str(hello(0))
            for index in range(100):
                await ws.send_str(msg("work", f"01JABCDEFGHJKMNPQRSTV{index:05d}"))
            return await until_pong(ws, r.phone)

    frames = asyncio.run(asyncio.wait_for(main(), 30))
    assert [f["type"] for f in frames] == ["welcome", "pong"]
    assert len(r.adapter.handed_on) == 100 and len(reads) == 1  # the load at start, none per frame
    assert TEXT not in " ".join(f"{record.getMessage()} {record.args!r}" for record in caplog.records)


def test_a_slow_read_holds_the_frame_that_met_it_and_runs_on_the_checks_own_worker(adapter_module, tmp_path):
    r = rig(adapter_module, tmp_path)
    readers: list[str] = []
    stall = threading.Event()

    def stalled() -> Any:
        readers.append(threading.current_thread().name)
        if stall.is_set():
            time.sleep(1.0)
        return r.w.registry.list()

    ids = revoked_ids(r, stalled)

    async def main() -> tuple[float, float, list[str]]:
        async with served(r, ids) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers(), autoping=False)
            await ws.send_str(hello(0))
            assert (await ws.receive_json(timeout=5))["type"] == "welcome"
            stall.set()
            r.w.registry.add("new phone", new_fingerprints(1)[0], ["work"])  # the signer changes the file
            started = time.monotonic()
            await ws.send_str(msg("work"))  # meets the 1 s re-read: held until it ends, then handed on
            gap, last = 0.0, time.monotonic()
            while not r.adapter.handed_on and time.monotonic() - started < 5:  # the loop keeps turning meanwhile
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gap, last = max(gap, now - last), now
            held = time.monotonic() - started
            frames = await until_pong(ws, r.phone)
            return held, gap, [f["type"] for f in frames]

    held, gap, types = asyncio.run(asyncio.wait_for(main(), 30))
    assert len(r.adapter.handed_on) == 1 and 0.9 <= held < 2.0 and gap < 0.1 and types == ["pong"]
    assert len(readers) == 2 and all(name.startswith("glasses-revoked-ids") for name in readers)
