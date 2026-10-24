# SPDX-License-Identifier: Apache-2.0
"""Device registry file safety: corrupt or hand-edited files fail closed, loose modes, symlinks, unsafe lock
files, swept temps, atomic-write failures and concurrent adds (``glasses.registry``; docs/architecture.md section 4)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from datetime import UTC, datetime

import pytest

from glasses import config as gcfg
from glasses import registry as reg

FP_A = "ab" * 32
FP_B = "cd" * 32
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "glasses"  # created by the registry itself


@pytest.fixture
def registry(state_dir):
    return reg.Registry(state_dir, clock=lambda: NOW)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _file_text(state_dir) -> str:
    return (state_dir / reg.FILE_NAME).read_text()


# -- file safety -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"version": 2, "devices": []}',
        b'{"version": 1}',
        b'{"version": 1, "devices": [1]}',
        b'{"version": 1, "devices": [{"deviceId": "x"}]}',
        b"\xff\xfe",
    ],
)
def test_corrupt_file_fails_closed(registry, state_dir, content):
    state_dir.mkdir(mode=0o700)
    path = state_dir / reg.FILE_NAME
    path.write_bytes(content)
    path.chmod(0o600)
    for call in (registry.list, lambda: registry.lookup("t", FP_A)[0], lambda: registry.add("P", FP_A, [])):
        with pytest.raises(reg.RegistryCorruptError):
            call()
    assert path.read_bytes() == content


def test_entry_with_active_status_but_no_hash_is_corrupt(registry, state_dir):
    device, _ = registry.add("Phone", FP_A, [])
    path = state_dir / reg.FILE_NAME
    data = json.loads(path.read_text())
    data["devices"][0]["tokenSha256"] = None
    path.write_text(json.dumps(data))
    with pytest.raises(reg.RegistryCorruptError):
        registry.list()


@pytest.mark.parametrize(
    "key, value",
    [
        ("lanes", ["../x"]),
        ("lanes", ["Work\r"]),
        ("lanes", ["work", "work"]),
        ("lanes", "work"),
        ("lanes", [f"l{i}" for i in range(gcfg.MAX_LANES + 1)]),
        ("lanes", [True]),
        ("name", "\u202eevil"),
        ("name", "a\x00b"),
        ("name", ""),
        ("name", " Phone"),
        ("name", "x" * 65),
        ("name", 7),
        ("version", True),
        ("version", 1.0),
    ],
)
def test_on_disk_entry_breaking_add_rules_is_corrupt(registry, state_dir, key, value):
    _, token = registry.add("Phone", FP_A, ["work"])
    path = state_dir / reg.FILE_NAME
    data = json.loads(path.read_text())
    (data if key == "version" else data["devices"][0])[key] = value
    path.write_text(json.dumps(data))
    for call in (registry.list, lambda: registry.lookup(token, FP_A)[0], lambda: registry.add("P", FP_B, [])):
        with pytest.raises(reg.RegistryCorruptError) as info:
            call()
        assert token not in str(info.value) and _sha(token) not in str(info.value)


@pytest.mark.parametrize("target, mode", [("file", 0o640), ("file", 0o604), ("dir", 0o750), ("dir", 0o701)])
def test_loose_permissions_refused(registry, state_dir, target, mode):
    _, token = registry.add("Phone", FP_A, [])
    (state_dir / reg.FILE_NAME if target == "file" else state_dir).chmod(mode)
    with pytest.raises(reg.RegistryPermissionError):
        registry.lookup(token, FP_A)[0]
    with pytest.raises(reg.RegistryPermissionError):
        registry.add("Other", FP_B, [])


def test_symlinked_file_refused(registry, state_dir, tmp_path):
    registry.add("Phone", FP_A, [])
    real = tmp_path / "elsewhere.json"
    (state_dir / reg.FILE_NAME).rename(real)
    (state_dir / reg.FILE_NAME).symlink_to(real)
    with pytest.raises(reg.RegistryPermissionError):
        registry.list()


@pytest.mark.parametrize("breakage", ["symlink", "mode"])
def test_unsafe_lock_file_refused(registry, state_dir, tmp_path, breakage):
    registry.add("Phone", FP_A, [])
    lock = state_dir / reg.LOCK_NAME
    if breakage == "symlink":
        lock.unlink()
        lock.symlink_to(tmp_path / "elsewhere.lock")
    else:
        lock.chmod(0o644)
    with pytest.raises(reg.RegistryPermissionError):
        registry.add("Other", FP_B, [])
    assert len(registry.list()) == 1


def test_stale_temp_from_a_crash_is_swept(registry, state_dir):
    registry.add("Phone", FP_A, [])
    stale = state_dir / f".{reg.FILE_NAME}.crash.tmp"
    stale.write_text("{}")
    registry.add("Other", FP_B, [])
    assert sorted(os.listdir(state_dir)) == [reg.FILE_NAME, reg.LOCK_NAME]


def test_dir_fsync_failure_after_rename_keeps_the_token(registry, monkeypatch, caplog):
    def boom(_path):
        raise OSError("io")

    monkeypatch.setattr(reg, "_fsync_dir", boom)
    device, token = registry.add("Phone", FP_A, [])
    assert registry.lookup(token, FP_A)[0] == device
    assert "fsync failed (OSError)" in caplog.text and token not in caplog.text


def test_missing_parent_dir_is_an_error(tmp_path):
    registry = reg.Registry(tmp_path / "no" / "such", clock=lambda: NOW)
    with pytest.raises(reg.RegistryError):
        registry.add("Phone", FP_A, [])


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_failed_write_leaves_old_file_and_no_temp(registry, state_dir, monkeypatch, failing):
    registry.add("Phone", FP_A, [])
    before = sorted(os.listdir(state_dir))
    old = _file_text(state_dir)

    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(reg.os, failing, boom)
    with pytest.raises(OSError):
        registry.add("Other", FP_B, [])
    monkeypatch.undo()
    assert sorted(os.listdir(state_dir)) == before
    assert _file_text(state_dir) == old


def test_write_fsyncs_file_and_directory(registry, monkeypatch):
    kinds: list[bool] = []
    real_fsync = os.fsync

    def spy(fd):
        kinds.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(reg.os, "fsync", spy)
    registry.add("Phone", FP_A, [])
    assert kinds == [False, True]


def test_concurrent_adds_from_two_registry_objects(state_dir):
    regs = [reg.Registry(state_dir, clock=lambda: NOW) for _ in range(2)]
    issued: list[tuple[str, str]] = []

    def worker(i: int) -> None:
        fp = f"{i:064x}"
        _, token = regs[i % 2].add(f"Phone {i}", fp, [])
        issued.append((token, fp))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(regs[0].list()) == 12
    assert all(regs[1].lookup(token, fp)[0] is not None for token, fp in issued)
