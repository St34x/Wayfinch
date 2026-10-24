# SPDX-License-Identifier: Apache-2.0
"""Registry limits that keep the store usable: ``add`` refuses to outgrow the read cap (so revoke never gets
locked out), a non-regular file in place of ``devices.json`` is refused at once, and a read is bounded by the cap
even when the file grew after the size check (``glasses.registry``; docs/architecture.md section 4)."""

from __future__ import annotations

import os
import signal
from datetime import UTC, datetime

import pytest

from glasses import registry as reg

FP_A, FP_B, FP_C = "ab" * 32, "cd" * 32, "ef" * 32
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
TOKEN = "t" * 43


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "glasses"


@pytest.fixture
def registry(state_dir):
    return reg.Registry(state_dir, clock=lambda: NOW)


@pytest.fixture
def deadline():
    """Turn a hang into a failure: SIGALRM interrupts a blocking open after a few seconds."""

    def _expired(signum, frame):
        raise TimeoutError("the registry call blocked")

    old = signal.signal(signal.SIGALRM, _expired)
    signal.alarm(5)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


def _data_path(state_dir):
    return state_dir / reg.FILE_NAME


def test_add_past_the_read_cap_is_refused_and_the_store_stays_usable(registry, state_dir, monkeypatch):
    first, token = registry.add("Phone", FP_A, ["work"])
    path = _data_path(state_dir)
    before = path.read_bytes()
    monkeypatch.setattr(reg, "MAX_FILE_BYTES", len(before) + 100)  # room for no second entry
    with pytest.raises(reg.RegistryFullError, match="registry full"):
        registry.add("Tablet", FP_B, ["work"])
    assert path.read_bytes() == before
    assert not list(state_dir.glob(".*.tmp"))
    assert [d.device_id for d in registry.list()] == [first.device_id]
    assert registry.lookup(token, FP_A)[0] == first
    assert registry.revoke(first.device_id).revoked_at is not None
    assert registry.lookup(token, FP_A)[0] is None
    assert registry.lookup("x" * 43, FP_A)[1]


def test_add_that_ends_exactly_at_the_cap_is_accepted(registry, state_dir, monkeypatch):
    registry.add("Phone", FP_A, ["work"])
    size_one = _data_path(state_dir).stat().st_size
    registry.add("Tablet", FP_B, ["work"])
    size_two = _data_path(state_dir).stat().st_size
    monkeypatch.setattr(reg, "MAX_FILE_BYTES", size_two)
    assert len(registry.list()) == 2
    with pytest.raises(reg.RegistryFullError):
        registry.add("Watch", FP_C, ["work"])
    assert size_one < size_two == _data_path(state_dir).stat().st_size


def test_refused_add_is_a_registry_error_without_content_in_the_message(registry, state_dir, monkeypatch):
    registry.add("Phone", FP_A, ["work"])
    monkeypatch.setattr(reg, "MAX_FILE_BYTES", _data_path(state_dir).stat().st_size + 100)
    with pytest.raises(reg.RegistryFullError) as info:
        registry.add("Tablet", FP_B, ["work"])
    assert isinstance(info.value, reg.RegistryError)
    assert FP_A not in str(info.value) and FP_B not in str(info.value)


@pytest.mark.parametrize("operation", ["list", "lookup-device", "lookup-tombstone", "add", "revoke"])
def test_a_directory_in_place_of_the_file_is_corrupt(registry, state_dir, operation):
    state_dir.mkdir(mode=0o700)
    _data_path(state_dir).mkdir()
    calls = {
        "list": registry.list,
        "lookup-device": lambda: registry.lookup(TOKEN, FP_A)[0],
        "lookup-tombstone": lambda: registry.lookup("x" * 43, FP_A)[1],
        "add": lambda: registry.add("Phone", FP_A, []),
        "revoke": lambda: registry.revoke("0" * 16),
    }
    with pytest.raises(reg.RegistryCorruptError):
        calls[operation]()


@pytest.mark.parametrize("operation", ["list", "lookup-device", "lookup-tombstone", "add", "revoke"])
def test_a_fifo_in_place_of_the_file_is_refused_at_once(registry, state_dir, deadline, operation):
    state_dir.mkdir(mode=0o700)
    os.mkfifo(_data_path(state_dir), 0o600)
    calls = {
        "list": registry.list,
        "lookup-device": lambda: registry.lookup(TOKEN, FP_A)[0],
        "lookup-tombstone": lambda: registry.lookup("x" * 43, FP_A)[1],
        "add": lambda: registry.add("Phone", FP_A, []),
        "revoke": lambda: registry.revoke("0" * 16),
    }
    with pytest.raises(reg.RegistryCorruptError):
        calls[operation]()


class _GrownFile:
    """The file object ``Registry._read`` opens, but whatever is asked for, a file longer than the cap arrives."""

    def __init__(self, fh, sink):
        self._fh, self._sink = fh, sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def fileno(self):
        return self._fh.fileno()

    def read(self, size=-1):
        self._sink.append(size)
        return b" " * (reg.MAX_FILE_BYTES + 1)


def test_a_file_that_grew_after_the_size_check_is_refused_after_a_bounded_read(registry, state_dir, monkeypatch):
    registry.add("Phone", FP_A, ["work"])
    asked: list[int] = []
    real_fdopen = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda fd, mode="r", *a, **k: _GrownFile(real_fdopen(fd, mode, *a, **k), asked))
    with pytest.raises(reg.RegistryCorruptError, match=str(reg.MAX_FILE_BYTES)):
        registry.list()
    assert asked == [reg.MAX_FILE_BYTES + 1]
