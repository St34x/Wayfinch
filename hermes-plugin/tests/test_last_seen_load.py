# SPDX-License-Identifier: Apache-2.0
"""The last-seen writer under load and failure (T1.179): many devices, a full disk, a writer that dies, a file that is
not a file, and a ``close`` that races a ``touch``. Display only: nothing here may touch the signer's file."""

from __future__ import annotations

import errno
import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from signer_registry_helpers import FP

from glasses.registry import Device
from glasses.registry_signer_file import LAST_SEEN_NAME, OVERLAY_NAME, LastSeen

T0 = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def device(n: int) -> Device:
    return Device(f"{n:016x}", "glasses", FP, ("main",), (), "2026-10-01T00:00:00Z", None, None)


def stored(tmp_path: Path) -> dict[str, str]:
    return dict(json.loads((tmp_path / "state" / LAST_SEEN_NAME).read_text())["seen"])


def test_hundreds_of_devices_from_many_threads_are_all_written_with_few_writes(tmp_path, monkeypatch):
    seen = LastSeen(tmp_path / "state", clock=Clock())
    writes: list[int] = []
    real = seen._write
    monkeypatch.setattr(seen, "_write", lambda doc: (writes.append(len(doc)), real(doc)))
    threads = [threading.Thread(target=lambda r=range(i, 400, 8): [seen.touch(device(n)) for n in r]) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    seen.close()
    assert set(stored(tmp_path)) == {f"{n:016x}" for n in range(400)}
    assert 1 <= len(writes) < 400 and writes[-1] == 400  # coalesced, and the last write has every device
    assert not [t for t in threading.enumerate() if t.name.startswith("glasses-last-seen")]  # no writer left behind
    assert sorted(os.listdir(tmp_path / "state")) == [LAST_SEEN_NAME]


def test_touches_that_arrive_while_the_writer_is_blocked_are_not_lost(tmp_path, monkeypatch):
    release, started = threading.Event(), threading.Event()
    seen = LastSeen(tmp_path / "state", clock=Clock())
    real = seen._write

    def slow(doc: dict[str, str]) -> None:
        started.set()
        release.wait(5)
        real(doc)

    monkeypatch.setattr(seen, "_write", slow)
    seen.touch(device(1))
    assert started.wait(5)
    seen.touch(device(2))
    seen.touch(device(3))
    release.set()
    seen.close()
    assert set(stored(tmp_path)) == {f"{n:016x}" for n in (1, 2, 3)}


def test_a_full_disk_is_logged_by_type_leaves_no_temp_file_and_the_next_minute_recovers(tmp_path, monkeypatch, caplog):
    clock = Clock()
    seen = LastSeen(tmp_path / "state", clock=clock)
    real_fsync = os.fsync

    def full(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with caplog.at_level(logging.WARNING):
        seen.touch(device(1))
        seen._pool.submit(lambda: None).result(5)
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert any("OSError" in r.getMessage() for r in caplog.records) and "No space" not in caplog.text
    assert not [n for n in os.listdir(tmp_path / "state") if n.endswith(".tmp")]
    clock.now = T0 + timedelta(seconds=61)
    seen.touch(device(2))
    seen.close()
    assert set(stored(tmp_path)) == {f"{n:016x}" for n in (1, 2)}  # the failed one is written with the next


def test_a_writer_that_raises_something_unexpected_does_not_stop_later_writes(tmp_path, monkeypatch):
    clock, tick = Clock(), [0.0]
    seen = LastSeen(tmp_path / "state", clock=clock, monotonic=lambda: tick[0])
    real = seen._write
    calls: list[int] = []

    def flaky(doc: dict[str, str]) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        real(doc)

    monkeypatch.setattr(seen, "_write", flaky)
    seen.touch(device(1))
    seen._pool.submit(lambda: None).result(5)
    clock.now = T0 + timedelta(seconds=61)
    tick[0] = 61.0  # the throttle runs on the monotonic clock
    seen.touch(device(1))
    seen.close()
    assert set(stored(tmp_path)) == {f"{1:016x}"}


def test_a_writer_that_raises_something_unexpected_is_logged(tmp_path, monkeypatch, caplog):
    seen = LastSeen(tmp_path / "state", clock=Clock())

    def broken(doc: dict[str, str]) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(seen, "_write", broken)
    with caplog.at_level(logging.WARNING):
        seen.touch(device(1))
        seen.close()
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversize", "loose_mode"])
def test_a_file_that_is_not_a_usable_last_seen_file_is_dropped_without_hanging_and_replaced(tmp_path, kind):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path, victim = state / LAST_SEEN_NAME, tmp_path / "victim.txt"
    victim.write_text("precious")
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "symlink":
        path.symlink_to(victim)
    else:
        path.write_bytes(b" " * (4 * 1024 * 1024 + 1) if kind == "oversize" else b'{"version":1,"seen":{}}')
        path.chmod(0o600 if kind == "oversize" else 0o644)
    made: list[LastSeen] = []
    worker = threading.Thread(target=lambda: made.append(LastSeen(state, clock=Clock())), daemon=True)
    worker.start()
    worker.join(5)
    assert made, "the constructor hung"
    made[0].touch(device(1))
    made[0].close()
    assert victim.read_text() == "precious"
    assert set(stored(tmp_path)) == {f"{1:016x}"}


def test_a_directory_in_place_of_the_file_is_a_logged_write_failure_not_a_crash(tmp_path, caplog):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / LAST_SEEN_NAME).mkdir(mode=0o700)
    with caplog.at_level(logging.WARNING):
        seen = LastSeen(state, clock=Clock())
        seen.touch(device(1))
        seen.close()
    assert any("glasses last-seen" in r.getMessage() for r in caplog.records)


def test_last_seen_never_writes_the_overlay_or_any_file_but_its_own(tmp_path):
    seen = LastSeen(tmp_path / "state", clock=Clock())
    for n in range(50):
        seen.touch(device(n))
    seen.close()
    assert sorted(os.listdir(tmp_path / "state")) == [LAST_SEEN_NAME]
    assert OVERLAY_NAME not in os.listdir(tmp_path / "state")


def test_touches_after_close_write_nothing_and_do_not_raise(tmp_path):
    seen = LastSeen(tmp_path / "state", clock=Clock())
    seen.close()
    seen.touch(device(1))
    seen.close()
    assert not (tmp_path / "state").exists() or LAST_SEEN_NAME not in os.listdir(tmp_path / "state")


class _CloseAfterUnlock:
    """A lock that, once, closes the writer right after ``touch`` released it: the other thread's ``close``."""

    def __init__(self, inner: Any, seen: LastSeen) -> None:
        self.inner, self.seen, self.armed = inner, seen, True

    def __enter__(self) -> Any:
        return self.inner.__enter__()

    def __exit__(self, *exc: object) -> Any:
        result = self.inner.__exit__(*exc)
        if self.armed:
            self.armed = False
            self.seen.close()
        return result


def test_a_close_from_another_thread_between_the_lock_and_the_submit_does_not_make_touch_raise(tmp_path):
    seen = LastSeen(tmp_path / "state", clock=Clock())
    seen._lock = _CloseAfterUnlock(seen._lock, seen)  # type: ignore[assignment]
    seen.touch(device(1))
