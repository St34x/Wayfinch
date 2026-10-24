# SPDX-License-Identifier: Apache-2.0
"""The frame check survives its own failures (T1.248): a poll step that raises is logged once by type and the poll
goes on, so a later revoke is still cut with no frame; a cut that raises for one device does not stop the cuts of the
other devices found by the same read; a read of a file whose stat failed is never taken as the file's version, so the
check refuses (``RegistryUnread``) instead of answering from the last set until a read with a stat succeeds."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from outbox_helpers import Dev
from revoked_ids_helpers import REVOKED, FakeFile, X, Y, settled

from glasses import revoked_ids
from glasses.revoked_ids import ReadPending, RegistryUnread, RevokedIds

Z = "3333333333333333"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_poll_step_that_raises_is_logged_once_and_the_next_revoke_is_still_cut(tmp_path, monkeypatch, caplog):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)
    real_key, raises = revoked_ids._key, {"left": 3}

    def key(path):
        if raises["left"] > 0:  # the first poll steps fail (an unexpected error, not an OSError of the stat)
            raises["left"] -= 1
            raise ValueError("injected")
        return real_key(path)

    async def main() -> list[str]:
        await ids.load()
        cuts: list[str] = []

        async def close(device_id: str, code: int) -> bool:
            cuts.append(device_id)
            return True

        monkeypatch.setattr(revoked_ids, "_key", key)
        ids.bind(close, lambda _id: None)
        deadline = time.monotonic() + 3
        while raises["left"] > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        f.devices = [Dev(X, revoked_at=REVOKED), Dev(Y, revoked_at=REVOKED)]
        f.write()
        deadline = time.monotonic() + 3
        while not cuts and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        poll = ids._poll
        alive = poll is not None and not poll.done()  # the poll did not end on the error
        ids.close()
        assert alive
        return cuts

    with caplog.at_level(logging.WARNING, logger="glasses"):
        assert asyncio.run(main()) == [Y]
    assert _warnings(caplog) == ["glasses revoked ids: poll step failed (ValueError); polling on"]


def test_a_cut_that_raises_for_one_device_still_cuts_the_others_of_the_same_read(tmp_path, caplog):
    f = FakeFile(tmp_path / "devices.json")
    f.devices = [Dev(X), Dev(Y), Dev(Z)]
    f.write()
    ids = RevokedIds(f.path, f.read)
    purged: list[str] = []

    def purge(device_id: str) -> None:
        if device_id == X:  # the first of the three (sorted) fails, e.g. the outbox pool is already shut down
            raise RuntimeError("cannot schedule new futures after shutdown")
        purged.append(device_id)

    async def main() -> list[str]:
        await ids.load()
        closed: list[str] = []

        async def close(device_id: str, code: int) -> bool:
            closed.append(device_id)
            return True

        ids.bind(close, purge)
        f.devices = [Dev(X, revoked_at=REVOKED), Dev(Y, revoked_at=REVOKED), Dev(Z, revoked_at=REVOKED)]
        f.write()
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)
        await settled(ids)
        await asyncio.sleep(0.05)  # the close tasks of the cuts
        ids.close()
        assert all(ids.is_revoked(d) for d in (X, Y, Z))  # every one stays revoked for the frame check
        return closed

    with caplog.at_level(logging.WARNING, logger="glasses"):
        assert asyncio.run(main()) == [Y, Z]
    assert purged == [Y, Z]
    assert _warnings(caplog) == ["glasses revoked ids: cutting a revoked device failed (RuntimeError)"]


def test_a_read_while_the_stat_fails_is_never_taken_as_read(tmp_path, monkeypatch):
    f = FakeFile(tmp_path / "devices.json")
    ids = RevokedIds(f.path, f.read)
    stat = {"fails": False}
    real_key = revoked_ids._key
    monkeypatch.setattr(revoked_ids, "_key", lambda path: None if stat["fails"] else real_key(path))

    async def main() -> bool:
        await ids.load()
        stat["fails"] = True  # e.g. the file is replaced while its directory cannot be searched
        with pytest.raises(ReadPending):
            ids.is_revoked(Y)  # a stat that differs from the version read: one read, which succeeds
        await settled(ids)
        for _ in range(2):  # refused while the stat keeps failing, never answered from the last set
            with pytest.raises(RegistryUnread):
                ids.is_revoked(Y)
        assert ids.is_revoked(X) is True  # a device already known as revoked stays revoked
        stat["fails"] = False  # the version read before: answered again, no read
        answer = ids.is_revoked(Y)
        ids.close()
        return answer

    assert asyncio.run(main()) is False and f.reads == 2
