# SPDX-License-Identifier: Apache-2.0
"""Device registry on a corrupt or hostile state dir: every kind of
corrupt on-disk entry, deep nesting, a huge file, unsafe lock files, stale temps, the no-early-return guard of
``verify``, and no token or token hash in any exception or log line. Open defects are strict xfails."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime

import pytest

from glasses import config as gcfg
from glasses import registry as reg

FP_A, FP_B, FP_C = "ab" * 32, "cd" * 32, "ef" * 32
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "glasses"


@pytest.fixture
def registry(state_dir):
    return reg.Registry(state_dir, clock=lambda: NOW)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _calls(registry, token):
    return {
        "list": registry.list,
        "verify": lambda: registry.verify(token, FP_A),
        "revoked_fingerprint": lambda: registry.revoked_fingerprint(FP_A),
        "add": lambda: registry.add("Other", FP_B, []),
    }


def _assert_all_corrupt(registry, token, caplog):
    for name, call in _calls(registry, token).items():
        with pytest.raises(reg.RegistryCorruptError) as info:
            call()
        assert token not in str(info.value) and _sha(token) not in str(info.value), name
    assert token not in caplog.text and _sha(token) not in caplog.text


def _edit(state_dir, mutate):
    path = state_dir / reg.FILE_NAME
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def _set(key, value):
    return lambda data: data["devices"][0].__setitem__(key, value)


def _top(key, value):
    return lambda data: data.__setitem__(key, value)


CORRUPT = {
    "fingerprint int": _set("certFingerprint", 5),
    "fingerprint upper-case": _set("certFingerprint", "AB" * 32),
    "fingerprint short": _set("certFingerprint", "ab" * 31),
    "deviceId int": _set("deviceId", 5),
    "deviceId not hex": _set("deviceId", "z" * 16),
    "features of ints": _set("features", [1]),
    "features a string": _set("features", "x"),
    "createdAt null": _set("createdAt", None),
    "lastSeen int": _set("lastSeen", 5),
    "revokedAt empty but token kept": _set("revokedAt", ""),
    "tokenSha256 upper-case": lambda d: d["devices"][0].__setitem__(
        "tokenSha256", d["devices"][0]["tokenSha256"].upper()
    ),
    "tokenSha256 int": _set("tokenSha256", 7),
    "lanes over MAX_LANES": _set("lanes", [f"l{i}" for i in range(gcfg.MAX_LANES + 1)]),
    "lane float": _set("lanes", [1.5]),
    "lane bool": _set("lanes", [False]),
    "lane with trailing newline": _set("lanes", ["work\n"]),
    "lane upper-case": _set("lanes", ["Work"]),
    "lanes null": _set("lanes", None),
    "name bool": _set("name", True),
    "name float": _set("name", 1.5),
    "name null": _set("name", None),
    "name with newline": _set("name", "Pho\nne"),
    "extra key": _set("x", 1),
    "missing key": lambda d: d["devices"][0].pop("lastSeen"),
    "version true": _top("version", True),
    "version 1.0": _top("version", 1.0),
    "version 2": _top("version", 2),
    "version string": _top("version", "1"),
    "extra top-level key": _top("x", 1),
    "devices a mapping": _top("devices", {}),
    "duplicate deviceId": lambda d: d["devices"].append({**d["devices"][0], "certFingerprint": FP_C}),
    "duplicate fingerprint": lambda d: d["devices"].append({**d["devices"][0], "deviceId": "0" * 16}),
}


@pytest.mark.parametrize("mutate", CORRUPT.values(), ids=CORRUPT.keys())
def test_every_kind_of_corrupt_entry_is_a_typed_error_everywhere(registry, state_dir, caplog, mutate):
    caplog.set_level(logging.DEBUG)
    _, token = registry.add("Phone", FP_A, ["work"])
    before = (state_dir / reg.FILE_NAME).read_bytes()
    _edit(state_dir, mutate)
    edited = (state_dir / reg.FILE_NAME).read_bytes()
    _assert_all_corrupt(registry, token, caplog)
    assert (state_dir / reg.FILE_NAME).read_bytes() == edited != before  # a failed add never rewrites it


@pytest.mark.xfail(
    strict=True,
    reason="known gap: certFingerprint null passes _valid_entry "
    "(_norm_fp(None) == None); verify then raises TypeError, list/add accept it",
)
def test_null_fingerprint_on_disk_is_corrupt(registry, state_dir, caplog):
    _, token = registry.add("Phone", FP_A, ["work"])
    _edit(state_dir, _set("certFingerprint", None))
    _assert_all_corrupt(registry, token, caplog)


def test_null_fingerprint_never_authenticates(registry, state_dir):
    """Whatever the error type, a null fingerprint must not let a token through (fail closed)."""
    _, token = registry.add("Phone", FP_A, ["work"])
    _edit(state_dir, _set("certFingerprint", None))
    try:
        assert registry.verify(token, FP_A) is None
    except Exception as exc:  # TypeError today (xfail above), RegistryCorruptError once fixed
        assert token not in str(exc) and _sha(token) not in str(exc)


@pytest.mark.parametrize(
    "text",
    [
        "[" * 10_000 + "]" * 10_000,
        '{"a":' * 100_000 + "1" + "}" * 100_000,
        "[" * 1_000_000 + "]" * 1_000_000,
        '{"version": 1, "devices": [' + "[" * 50_000 + "]" * 50_000 + "]}",
    ],
    ids=["list-10k", "object-100k", "list-1M", "inside-devices-50k"],
)
def test_deep_nesting_is_a_typed_corrupt_error(registry, state_dir, caplog, text):
    _, token = registry.add("Phone", FP_A, ["work"])
    (state_dir / reg.FILE_NAME).write_text(text)
    _assert_all_corrupt(registry, token, caplog)


@pytest.mark.parametrize(
    "raw",
    [b"", b"\xff\xfe{", b'{"version": NaN, "devices": []}', b"null", b"[]"],
    ids=["empty", "bad-utf8", "nan", "null", "list"],
)
def test_odd_documents_are_typed_corrupt_errors(registry, state_dir, caplog, raw):
    _, token = registry.add("Phone", FP_A, ["work"])
    (state_dir / reg.FILE_NAME).write_bytes(raw)
    _assert_all_corrupt(registry, token, caplog)


def test_huge_file_is_rejected_without_hanging(registry, state_dir, caplog):
    _, token = registry.add("Phone", FP_A, ["work"])
    junk = ",".join(['"' + "x" * 1000 + '"'] * 20_000)  # ~20 MB of non-entries
    (state_dir / reg.FILE_NAME).write_text('{"version": 1, "devices": [' + junk + "]}")
    start = time.monotonic()
    _assert_all_corrupt(registry, token, caplog)
    assert time.monotonic() - start < 30


def test_registry_lane_bound_is_the_config_bound(registry, state_dir):
    assert reg.MAX_LANES is gcfg.MAX_LANES and reg.LANE_RE is gcfg.LANE_RE
    _, token = registry.add("Phone", FP_A, [f"l{i}" for i in range(gcfg.MAX_LANES)])
    assert registry.verify(token, FP_A) is not None
    with pytest.raises(reg.InvalidDeviceError):
        registry.add("Other", FP_B, [f"l{i}" for i in range(gcfg.MAX_LANES + 1)])


# -- lock file -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["directory", "fifo", "dangling-symlink", "mode-0604", "mode-0660"])
def test_unsafe_lock_file_refused_and_nothing_written(registry, state_dir, tmp_path, kind):
    registry.add("Phone", FP_A, [])
    lock = state_dir / reg.LOCK_NAME
    target = tmp_path / "elsewhere.lock"
    if kind in ("directory", "fifo", "dangling-symlink"):
        lock.unlink()
        {
            "directory": lambda: lock.mkdir(),
            "fifo": lambda: os.mkfifo(lock),
            "dangling-symlink": lambda: lock.symlink_to(target),
        }[kind]()
    else:
        lock.chmod(int(kind[-4:], 8))
    before = (state_dir / reg.FILE_NAME).read_bytes()
    with pytest.raises(reg.RegistryPermissionError):
        registry.add("Other", FP_B, [])
    assert (state_dir / reg.FILE_NAME).read_bytes() == before and not target.exists()
    assert len(registry.list()) == 1  # reads do not need the lock


# -- stale temps -----------------------------------------------------------------------------------


def test_sweep_unlinks_a_symlinked_temp_not_its_target(registry, state_dir, tmp_path):
    registry.add("Phone", FP_A, [])
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (state_dir / f".{reg.FILE_NAME}.evil.tmp").symlink_to(victim)
    registry.add("Other", FP_B, [])
    assert victim.read_text() == "keep me"
    assert sorted(os.listdir(state_dir)) == [reg.FILE_NAME, reg.LOCK_NAME]


def test_sweep_survives_a_temp_directory_and_keeps_other_files(registry, state_dir):
    registry.add("Phone", FP_A, [])
    (state_dir / f".{reg.FILE_NAME}.dir.tmp").mkdir()
    (state_dir / "notes.tmp").write_text("x")
    (state_dir / f".{reg.FILE_NAME}.bak").write_text("x")
    registry.add("Other", FP_B, [])
    assert len(registry.list()) == 2
    assert {"notes.tmp", f".{reg.FILE_NAME}.bak", f".{reg.FILE_NAME}.dir.tmp"} <= set(os.listdir(state_dir))


def test_reads_never_sweep(registry, state_dir):
    registry.add("Phone", FP_A, [])
    stale = state_dir / f".{reg.FILE_NAME}.crash.tmp"
    stale.write_text("{}")
    registry.list()
    registry.verify("x" * 43, FP_A)
    assert stale.exists()  # only writers (under the lock) sweep


# -- constant-time verify: no early return -----------------------------------------------------------


@pytest.fixture
def spied(registry, monkeypatch):
    issued = [registry.add(f"Phone {i}", fp, []) for i, fp in enumerate((FP_A, FP_B, FP_C))]
    seen: list[tuple[str, str]] = []

    def spy(a, b):
        seen.append((a, b))
        return a == b

    monkeypatch.setattr(reg.hmac, "compare_digest", spy)
    return issued, seen


@pytest.mark.parametrize("case", ["last-entry", "no-match", "right-token-wrong-fp", "wrong-token-right-fp"])
def test_verify_compares_every_entry_in_every_outcome(spied, registry, case):
    issued, seen = spied
    (_, t0), (_, t1), (last, t2) = issued
    token, fp, want = {
        "last-entry": (t2, FP_C, last),
        "no-match": ("Q" * 43, "11" * 32, None),
        "right-token-wrong-fp": (t2, FP_A, None),
        "wrong-token-right-fp": (t0, FP_B, None),
    }[case]
    assert registry.verify(token, fp) == want
    assert len(seen) == 2 * len(issued)
    assert all(token not in pair for pair in seen) and all(t not in pair for pair in seen for t in (t0, t1, t2))


# -- no token or hash in any log line --------------------------------------------------------------------


def test_no_token_or_hash_in_logs_over_a_lifecycle(registry, state_dir, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    device, token = registry.add("Phone", FP_A, ["work"])
    registry.verify(token, FP_A)
    registry.verify(token, FP_B)
    monkeypatch.setattr(reg, "_fsync_dir", lambda _p: (_ for _ in ()).throw(OSError("io")))
    registry.revoke(device.device_id)
    registry.revoked_fingerprint(FP_A)
    registry.list()
    assert caplog.records and token not in caplog.text and _sha(token) not in caplog.text
