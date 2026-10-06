# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the device registry (``glasses.registry``, T1.05; docs/architecture.md section 4)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
from datetime import UTC, datetime

import pytest

from glasses import config as gcfg
from glasses import registry as reg

FP_A = "ab" * 32
FP_B = "cd" * 32
FP_C = "ef" * 32
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "glasses"  # created by the registry itself


@pytest.fixture
def registry(state_dir):
    return reg.Registry(state_dir, clock=lambda: NOW)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _file_text(state_dir) -> str:
    return (state_dir / reg.FILE_NAME).read_text()


# -- add / verify / list ---------------------------------------------------------------------


def test_add_then_verify_and_list(registry):
    device, token = registry.add("Phone", FP_A, ["assistant", "work"])
    assert len(token) >= 43  # 256 random bits, url-safe base64
    assert registry.verify(token, FP_A) == device
    assert registry.verify(token, FP_A.upper()) == device
    assert device.lanes == ("assistant", "work")
    assert device.created_at == "2026-10-05T12:00:00Z"
    assert device.revoked_at is None and device.last_seen is None
    assert registry.list() == [device]


def test_file_holds_hash_never_token_with_modes(registry, state_dir):
    _, token = registry.add("Phone", FP_A, [])
    text = _file_text(state_dir)
    assert token not in text
    assert _sha(token) in text
    assert _mode(state_dir / reg.FILE_NAME) == 0o600
    assert _mode(state_dir) == 0o700


def test_two_devices_get_distinct_ids_and_tokens(registry):
    a, tok_a = registry.add("A", FP_A, [])
    b, tok_b = registry.add("B", FP_B, [])
    assert a.device_id != b.device_id and tok_a != tok_b
    assert registry.verify(tok_a, FP_B) is None  # token of A with the cert of B
    assert registry.verify(tok_b, FP_B) == b


@pytest.mark.parametrize(
    "token, fp",
    [
        ("wrong-token", FP_A),
        (None, FP_A),
        ("", FP_A),
        (b"x", FP_A),
        ("tok\n", FP_A),
        ("x" * 300, FP_A),
        ("TOKEN", FP_B),
        ("TOKEN", "zz" * 32),
        ("TOKEN", None),
        ("TOKEN", FP_A[:-1]),
        ("TOKEN", FP_A + "\n"),
    ],
)
def test_verify_negatives(registry, token, fp):
    _, real = registry.add("Phone", FP_A, [])
    token = real if token == "TOKEN" else token
    assert registry.verify(token, fp) is None


def test_verify_unknown_on_empty_registry(registry, state_dir):
    assert registry.verify("anything", FP_A) is None
    assert registry.list() == []
    assert not state_dir.exists()  # reads never create anything


def test_verify_compares_every_entry_without_early_return(registry, monkeypatch):
    fps = (FP_A, FP_B, FP_C)
    issued = [registry.add(f"Phone {i}", fp, []) for i, fp in enumerate(fps)]
    seen: list[tuple[str, str]] = []

    def spy(a, b):
        seen.append((a, b))
        return a == b

    monkeypatch.setattr(reg.hmac, "compare_digest", spy)
    first, token = issued[0]
    assert registry.verify(token, FP_A) == first  # matches the FIRST entry: the loop must still run on
    assert len(seen) == 2 * len(fps)  # token + fingerprint for every entry
    assert {a for a, _ in seen} >= set(fps)
    assert (_sha(token), _sha(token)) in seen
    assert all(token not in pair for pair in seen)
    monkeypatch.undo()
    registry.revoke(first.device_id)
    monkeypatch.setattr(reg.hmac, "compare_digest", spy)
    seen.clear()
    assert registry.verify(token, FP_A) is None  # revoked entries are compared too, not skipped
    assert len(seen) == 2 * len(fps)
    assert {a for a, _ in seen} >= set(fps)


# -- revoke / tombstone ----------------------------------------------------------------------


def test_revoke_kills_token_and_keeps_fingerprint_tombstone(registry, state_dir):
    device, token = registry.add("Phone", FP_A, ["work"])
    revoked = registry.revoke(device.device_id)
    assert revoked.revoked_at == "2026-10-05T12:00:00Z"
    assert registry.verify(token, FP_A) is None
    assert registry.revoked_fingerprint(FP_A) is True
    assert registry.revoked_fingerprint(FP_A.upper()) is True
    entry = json.loads(_file_text(state_dir))["devices"][0]
    assert entry["tokenSha256"] is None and entry["certFingerprint"] == FP_A
    assert _sha(token) not in _file_text(state_dir)
    assert registry.list() == [revoked]


def test_revoked_fingerprint_false_for_active_unknown_and_garbage(registry):
    registry.add("Phone", FP_A, [])
    for fp in (FP_A, FP_B, "nope", None):
        assert registry.revoked_fingerprint(fp) is False


def test_revoke_is_idempotent_and_keeps_first_time(state_dir):
    times = iter([NOW, datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 7, tzinfo=UTC)])
    registry = reg.Registry(state_dir, clock=lambda: next(times))
    device, _ = registry.add("Phone", FP_A, [])
    first = registry.revoke(device.device_id)
    assert registry.revoke(device.device_id).revoked_at == first.revoked_at == "2026-10-06T00:00:00Z"


def test_revoke_unknown_device(registry):
    with pytest.raises(reg.UnknownDeviceError):
        registry.revoke("missing")


def test_fingerprint_never_enrolled_twice(registry):
    device, _ = registry.add("Phone", FP_A, [])
    with pytest.raises(reg.InvalidDeviceError):
        registry.add("Again", FP_A.upper(), [])
    registry.revoke(device.device_id)
    with pytest.raises(reg.InvalidDeviceError):  # a revoked key never comes back
        registry.add("Again", FP_A, [])


# -- input validation ------------------------------------------------------------------------


def test_lanes_bound_comes_from_config(registry):
    assert reg.MAX_LANES is gcfg.MAX_LANES
    lanes = [f"l{i}" for i in range(gcfg.MAX_LANES)]
    registry.add("Max", FP_A, lanes)
    with pytest.raises(reg.InvalidDeviceError):
        registry.add("Over", FP_B, lanes + ["one_more"])


@pytest.mark.parametrize(
    "lanes",
    [
        "work",
        ("work",),
        ["Work"],
        ["work\n"],
        [""],
        [1],
        ["a" * 33],
        [" work"],
        ["work", "work"],
        None,
    ],
)
def test_bad_lanes_refused(registry, lanes, state_dir):
    with pytest.raises(reg.InvalidDeviceError):
        registry.add("Phone", FP_A, lanes)
    assert not (state_dir / reg.FILE_NAME).exists()


@pytest.mark.parametrize("name", ["", " Phone", "Phone\n", "a\x00b", "x" * 65, None, 3])
def test_bad_names_refused(registry, name):
    with pytest.raises(reg.InvalidDeviceError):
        registry.add(name, FP_A, [])


@pytest.mark.parametrize("fp", ["", "ab" * 31, "zz" * 32, "ab:" * 31 + "ab", None, FP_A + " "])
def test_bad_fingerprints_refused(registry, fp):
    with pytest.raises(reg.InvalidDeviceError):
        registry.add("Phone", fp, [])


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
    for call in (registry.list, lambda: registry.verify("t", FP_A), lambda: registry.add("P", FP_A, [])):
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
    for call in (registry.list, lambda: registry.verify(token, FP_A), lambda: registry.add("P", FP_B, [])):
        with pytest.raises(reg.RegistryCorruptError) as info:
            call()
        assert token not in str(info.value) and _sha(token) not in str(info.value)


@pytest.mark.parametrize("target, mode", [("file", 0o640), ("file", 0o604), ("dir", 0o750), ("dir", 0o701)])
def test_loose_permissions_refused(registry, state_dir, target, mode):
    _, token = registry.add("Phone", FP_A, [])
    (state_dir / reg.FILE_NAME if target == "file" else state_dir).chmod(mode)
    with pytest.raises(reg.RegistryPermissionError):
        registry.verify(token, FP_A)
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
    assert registry.verify(token, FP_A) == device
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
    assert all(regs[1].verify(token, fp) is not None for token, fp in issued)


# -- no secrets in logs or errors ------------------------------------------------------------


def test_no_token_or_hash_in_logs_errors_or_repr(registry, state_dir, caplog):
    caplog.set_level(logging.DEBUG)
    device, token = registry.add("Phone", FP_A, ["work"])
    secrets_ = (token, _sha(token))
    registry.verify(token, FP_A)
    registry.verify(token, FP_B)
    errors = []
    for call in (
        lambda: registry.add("Again", FP_A, []),
        lambda: registry.revoke(token),
        lambda: registry.add(token + "\n", FP_B, []),
        lambda: registry.add("P", FP_B, [token]),
    ):
        with pytest.raises(reg.RegistryError) as info:
            call()
        errors.append(info.value)
    (state_dir / reg.FILE_NAME).chmod(0o644)
    with pytest.raises(reg.RegistryPermissionError) as info:
        registry.verify(token, FP_A)
    errors.append(info.value)
    (state_dir / reg.FILE_NAME).chmod(0o600)
    registry.revoke(device.device_id)
    texts = [caplog.text, repr(device), *(str(e) for e in errors), *(repr(e) for e in errors)]
    assert device.device_id in caplog.text  # events are logged, by id only
    assert not [s for s in secrets_ for text in texts if s in text]
