# SPDX-License-Identifier: Apache-2.0
"""The plugin's ``last-seen.json`` (T1.179): written on a successful upgrade at most once a minute per device, off
the event loop, never into the signer's ``devices.json``."""

from __future__ import annotations

import json
import logging
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from signer_registry_helpers import FP, SIGNER_UID, publish

from glasses.registry import Device
from glasses.registry_signer_file import LAST_SEEN_NAME, DenyOverlay, LastSeen, SignerFileRegistry

T0 = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


class Clock:
    """The wall clock; ``monotonic`` follows it unless a test pins ``mono`` (a wall clock that is set back)."""

    def __init__(self) -> None:
        self.now = T0
        self.mono: float | None = None

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return (self.now - T0).total_seconds() if self.mono is None else self.mono


def seen_at(state: Path, clock: Clock | None = None) -> LastSeen:
    clock = clock or Clock()
    return LastSeen(state, clock=clock, monotonic=clock.monotonic)


def device(device_id: str = "0123456789abcdef") -> Device:
    return Device(device_id, "glasses", FP, ("main",), (), "2026-10-01T00:00:00Z", None, None)


def stored(tmp_path: Path) -> dict[str, str]:
    doc = json.loads((tmp_path / "state" / LAST_SEEN_NAME).read_text())
    assert doc["version"] == 1
    return dict(doc["seen"])


def test_a_touch_is_written_as_a_0600_file_with_only_the_id_and_the_time(tmp_path):
    seen = seen_at(tmp_path / "state")
    seen.touch(device())
    seen.close()
    assert stored(tmp_path) == {"0123456789abcdef": "2026-10-17T12:00:00Z"}
    path = tmp_path / "state" / LAST_SEEN_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_at_most_one_write_per_minute_per_device(tmp_path, monkeypatch):
    clock = Clock()
    seen = seen_at(tmp_path / "state", clock)
    real, writes = seen._write, []
    monkeypatch.setattr(seen, "_write", lambda doc: (writes.append(dict(doc)), real(doc)))
    seen.touch(device())
    clock.now = T0 + timedelta(seconds=59)
    seen.touch(device())
    seen.close()
    assert len(writes) == 1
    assert stored(tmp_path) == {"0123456789abcdef": "2026-10-17T12:00:00Z"}


def test_a_minute_later_and_another_device_are_written(tmp_path):
    clock = Clock()
    seen = seen_at(tmp_path / "state", clock)
    seen.touch(device())
    clock.now = T0 + timedelta(seconds=60)
    seen.touch(device())
    seen.touch(device("fedcba9876543210"))
    seen.close()
    assert stored(tmp_path) == {"0123456789abcdef": "2026-10-17T12:01:00Z", "fedcba9876543210": "2026-10-17T12:01:00Z"}


def test_a_wall_clock_set_back_does_not_freeze_the_stamp_once_a_minute_has_passed(tmp_path):
    clock = Clock()
    seen = seen_at(tmp_path / "state", clock)
    seen.touch(device())
    clock.now, clock.mono = T0 - timedelta(hours=1), 60.0  # the wall clock went back, a minute went by
    seen.touch(device())
    seen.close()
    assert stored(tmp_path) == {"0123456789abcdef": "2026-10-17T11:00:00Z"}


def test_a_wall_clock_that_steps_back_on_every_call_gives_one_write_per_minute(tmp_path, monkeypatch):
    clock = Clock()
    seen = seen_at(tmp_path / "state", clock)
    real, writes = seen._write, []
    monkeypatch.setattr(seen, "_write", lambda doc: (writes.append(dict(doc)), real(doc)))
    for step in range(20):
        clock.now, clock.mono = T0 - timedelta(hours=step), 5.0  # monotonic: no minute has passed
        seen.touch(device())
    seen.close()
    assert len(writes) == 1
    assert stored(tmp_path) == {"0123456789abcdef": "2026-10-17T12:00:00Z"}


def test_touch_does_not_wait_for_the_disk(tmp_path, monkeypatch):
    release, started = threading.Event(), threading.Event()
    seen = seen_at(tmp_path / "state")
    real = seen._write

    def slow(doc: dict[str, str]) -> None:
        started.set()
        release.wait(5)
        real(doc)

    monkeypatch.setattr(seen, "_write", slow)
    begun = time.monotonic()
    seen.touch(device())
    assert time.monotonic() - begun < 0.5 and started.wait(5)  # returned while the writer is still blocked
    release.set()
    seen.close()
    assert stored(tmp_path) == {"0123456789abcdef": "2026-10-17T12:00:00Z"}


def test_a_close_from_another_thread_during_touch_makes_touch_raise_nothing(tmp_path, monkeypatch):
    seen = seen_at(tmp_path / "state")
    real_submit, closers = seen._pool.submit, []

    def submit_after_a_close(*args, **kwargs):
        closer = threading.Thread(target=seen.close)
        closer.start()
        closer.join(0.3)  # a close that touch does not hold off has shut the pool down by now
        closers.append(closer)
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(seen._pool, "submit", submit_after_a_close)
    seen.touch(device())
    closers[0].join(5)
    assert not closers[0].is_alive()
    assert stored(tmp_path) == {"0123456789abcdef": "2026-10-17T12:00:00Z"}


def test_an_existing_file_is_kept_and_extended(tmp_path):
    earlier = seen_at(tmp_path / "state")
    earlier.touch(device("fedcba9876543210"))
    earlier.close()
    seen = seen_at(tmp_path / "state")
    seen.touch(device())
    seen.close()
    assert set(stored(tmp_path)) == {"0123456789abcdef", "fedcba9876543210"}


@pytest.mark.parametrize("content", [b"", b"x", b"[]", b'{"version":1}', b'{"version":1,"seen":{"bad":"t"}}'])
def test_a_corrupt_file_is_display_only_and_is_replaced(tmp_path, content):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / LAST_SEEN_NAME).write_bytes(content)
    (state / LAST_SEEN_NAME).chmod(0o600)
    seen = seen_at(state)
    seen.touch(device())
    seen.close()
    assert stored(tmp_path) == {"0123456789abcdef": "2026-10-17T12:00:00Z"}


def test_a_write_failure_is_logged_by_type_and_never_raised(tmp_path, caplog):
    seen = seen_at(tmp_path / "missing" / "state")  # the parent does not exist
    with caplog.at_level(logging.WARNING):
        seen.touch(device())
        seen.close()
    assert any("FileNotFoundError" in r.getMessage() for r in caplog.records)
    assert "0123456789abcdef" not in caplog.text


def test_an_unexpected_error_in_the_writer_is_logged_once_by_type_and_the_worker_lives(tmp_path, caplog, monkeypatch):
    seen = seen_at(tmp_path / "state")
    real, failed = seen._write, threading.Event()

    def flaky(doc: dict[str, str]) -> None:
        if not failed.is_set():
            failed.set()
            raise ValueError("0123456789abcdef is not a record")  # a message with an id in it
        real(doc)

    monkeypatch.setattr(seen, "_write", flaky)
    with caplog.at_level(logging.WARNING):
        seen.touch(device())
        assert failed.wait(5)
        seen.touch(device("fedcba9876543210"))  # the worker is alive: this one is written
        seen.close()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "ValueError" in warnings[0].getMessage()
    assert "0123456789abcdef" not in caplog.text
    assert "fedcba9876543210" in stored(tmp_path)


def test_the_signers_devices_json_is_never_written_and_a_touch_leaves_the_registry_unchanged(tmp_path):
    target, dev, token = publish(tmp_path)
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    seen = seen_at(tmp_path / "state")
    seen.touch(dev)
    seen.close()
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    registry = SignerFileRegistry(tmp_path / "public", SIGNER_UID, DenyOverlay(tmp_path / "state"))
    assert registry.lookup(token, FP)[0] == dev
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == [LAST_SEEN_NAME]


def test_the_docstrings_of_close_and_deny_tell_the_caller_where_not_to_call_them():
    def words(doc):
        return " ".join(doc.split())

    assert "blocks until the write in flight ends; call it off the loop" in words(LastSeen.close.__doc__)
    assert "never from the event loop; the memory update comes first" in words(DenyOverlay.deny.__doc__)
