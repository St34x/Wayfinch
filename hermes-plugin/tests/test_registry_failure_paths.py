# SPDX-License-Identifier: Apache-2.0
"""Device registry failure paths: cross-process adds, on-disk tombstone, atomic-write failures, odd verify
inputs, no secret in messages, and hand-edited or deeply nested files as typed corrupt errors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glasses import config as gcfg
from glasses import registry as reg

FP_A = "ab" * 32
FP_B = "cd" * 32
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
SENTINEL = "QAsentinelTOKEN_" + "z" * 27  # url-safe, 43 chars like a real token
PLUGIN_ROOT = Path(__file__).resolve().parents[1]

WORKER = """
import json, sys
from glasses import registry as reg
r = reg.Registry(sys.argv[1])
start, count = int(sys.argv[2]), int(sys.argv[3])
for i in range(start, start + count):
    fp = f"{i:064x}"
    _, token = r.add(f"Phone {i}", fp, ["work"])
    print(json.dumps([token, fp]), flush=True)
"""


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "glasses"


@pytest.fixture
def registry(state_dir):
    return reg.Registry(state_dir, clock=lambda: NOW)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# -- concurrency across processes -----------------------------------------------------------------


def test_concurrent_adds_from_four_processes_lose_nothing(tmp_path, state_dir):
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    env = {**os.environ, "PYTHONPATH": str(PLUGIN_ROOT)}
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(state_dir), str(k * 100), "10"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        for k in range(4)
    ]
    issued = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        issued += [json.loads(line) for line in out.splitlines()]
    assert len(issued) == 40
    r = reg.Registry(state_dir)
    assert len(r.list()) == 40
    assert all(r.verify(token, fp) is not None for token, fp in issued)
    assert sorted(os.listdir(state_dir)) == [reg.FILE_NAME, reg.LOCK_NAME]  # no temp left


# -- lanes ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lanes",
    [
        ["work\r"],
        ["work\t"],
        ["work\n"],
        ["work\r\n"],
        ["wörk"],
        ["work "],
        ["1work"],
        ["-work"],
        ["a", "b", "a"],
        ["work", "Work", "work"],
    ],
)
def test_more_bad_lanes_refused_without_echo(registry, state_dir, lanes):
    with pytest.raises(reg.InvalidDeviceError) as info:
        registry.add("Phone", FP_A, lanes)
    assert not any(lane in str(info.value) for lane in lanes if lane not in ("a", "b"))
    assert not (state_dir / reg.FILE_NAME).exists()


def test_lane_rule_is_the_envelope_lane_pattern(registry):
    # MAX_LANES == welcome.lanes.maxItems is T1.66's contract test (config side); here: same regex as the schema.
    schema = json.loads((PLUGIN_ROOT.parent / "protocol" / "envelope.schema.json").read_text(encoding="utf-8"))
    assert reg.LANE_RE is gcfg.LANE_RE and reg.MAX_LANES is gcfg.MAX_LANES
    assert f"^{reg.LANE_RE.pattern}$" == schema["$defs"]["lane"]["pattern"]
    lanes = [f"l{i}" for i in range(reg.MAX_LANES)]
    device, _ = registry.add("Max", FP_A, lanes)
    assert len(device.lanes) == reg.MAX_LANES
    with pytest.raises(reg.InvalidDeviceError):
        registry.add("Over", FP_B, lanes + ["x"])


# -- verify with odd inputs ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",
        "é" * 43,
        "x" * 257,
        "x" * 1_000_000,
        " TOKEN",
        "TOKEN ",
        "TOKEN\r",
        "TOKEN\x00",
        0,
        1.5,
        ["TOKEN"],
        {"t": 1},
        b"TOKEN",
    ],
)
def test_verify_odd_tokens_return_none(registry, token):
    _, real = registry.add("Phone", FP_A, [])
    if isinstance(token, str):
        token = token.replace("TOKEN", real)
    assert registry.verify(token, FP_A) is None


@pytest.mark.parametrize("fp", [None, "", " " + FP_A, "ＡＢ" * 32, "ab:" * 31 + "ab", 0, b"ab" * 32, ["x"]])
def test_verify_odd_fingerprints_return_none(registry, fp):
    _, token = registry.add("Phone", FP_A, [])
    assert registry.verify(token, fp) is None
    assert registry.revoked_fingerprint(fp) is False


# -- revoke: tombstone on disk ------------------------------------------------------------------


def test_revoke_tombstone_on_disk(registry, state_dir):
    keep, keep_tok = registry.add("Keep", FP_B, ["work"])
    gone, gone_tok = registry.add("Gone", FP_A, ["work"])
    registry.revoke(gone.device_id)
    raw = (state_dir / reg.FILE_NAME).read_text()
    entries = {e["deviceId"]: e for e in json.loads(raw)["devices"]}
    tomb = entries[gone.device_id]
    assert tomb["revokedAt"] == "2026-10-05T12:00:00Z"
    assert tomb["tokenSha256"] is None and tomb["certFingerprint"] == FP_A
    assert _sha(gone_tok) not in raw and gone_tok not in raw
    assert entries[keep.device_id]["tokenSha256"] == _sha(keep_tok)
    assert registry.verify(gone_tok, FP_A) is None
    assert registry.verify(keep_tok, FP_B) == keep
    assert registry.revoked_fingerprint(FP_A) is True and registry.revoked_fingerprint(FP_B) is False
    assert _mode(state_dir / reg.FILE_NAME) == 0o600 and _mode(state_dir) == 0o700


# -- atomic write: failure paths ------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_failure_on_first_write_leaves_nothing(registry, state_dir, monkeypatch, failing):
    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(reg.os, failing, boom)
    with pytest.raises(OSError):
        registry.add("Phone", FP_A, [])
    monkeypatch.undo()
    assert not (state_dir / reg.FILE_NAME).exists()
    assert [n for n in os.listdir(state_dir) if n.endswith(".tmp")] == []


def test_failure_on_revoke_write_keeps_device_active(registry, state_dir, monkeypatch):
    device, token = registry.add("Phone", FP_A, [])
    before = (state_dir / reg.FILE_NAME).read_bytes()
    monkeypatch.setattr(reg.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("io")))
    with pytest.raises(OSError):
        registry.revoke(device.device_id)
    monkeypatch.undo()
    assert (state_dir / reg.FILE_NAME).read_bytes() == before
    assert [n for n in os.listdir(state_dir) if n.endswith(".tmp")] == []


def test_modes_hold_with_permissive_umask(state_dir):
    old = os.umask(0)
    try:
        reg.Registry(state_dir).add("Phone", FP_A, [])
    finally:
        os.umask(old)
    assert _mode(state_dir) == 0o700
    assert _mode(state_dir / reg.FILE_NAME) == 0o600
    assert _mode(state_dir / reg.LOCK_NAME) == 0o600


# -- no secret in messages on failure paths ------------------------------------------------------


def test_known_token_never_in_messages_when_add_fails(registry, state_dir, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    registry.add("First", FP_A, [])
    monkeypatch.setattr(reg.secrets, "token_urlsafe", lambda _n: SENTINEL)
    texts = [caplog.text]
    for breakage in ("perm", "replace"):
        if breakage == "perm":
            state_dir.chmod(0o750)
        else:
            state_dir.chmod(0o700)
            monkeypatch.setattr(reg.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("io")))
        with pytest.raises((OSError, reg.RegistryError)) as info:
            registry.add("Second", FP_B, [])
        texts += [str(info.value), repr(info.value), caplog.text]
    state_dir.chmod(0o700)
    secrets_ = (SENTINEL, _sha(SENTINEL))
    assert not [s for s in secrets_ for t in texts if s in t]


def test_corrupt_file_holding_a_hash_never_echoes_it(registry, state_dir):
    _, token = registry.add("Phone", FP_A, [])
    path = state_dir / reg.FILE_NAME
    data = json.loads(path.read_text())
    data["devices"][0]["extra"] = token  # malformed entry that also carries secrets
    path.write_text(json.dumps(data)[:-3])  # and truncated JSON
    for call in (registry.list, lambda: registry.verify(token, FP_A), lambda: registry.revoked_fingerprint(FP_A)):
        with pytest.raises(reg.RegistryCorruptError) as info:
            call()
        assert token not in str(info.value) and _sha(token) not in str(info.value)


# -- hand-edited or hostile files on disk ---------------------------------------------------------


def test_deeply_nested_file_is_a_typed_corrupt_error(registry, state_dir):
    registry.add("Phone", FP_A, [])
    path = state_dir / reg.FILE_NAME
    path.write_bytes(b"[" * 200_000)
    with pytest.raises(reg.RegistryCorruptError):
        registry.list()


def test_hand_edited_bad_lane_on_disk_is_corrupt(registry, state_dir):
    _, token = registry.add("Phone", FP_A, ["work"])
    path = state_dir / reg.FILE_NAME
    data = json.loads(path.read_text())
    data["devices"][0]["lanes"] = ["Work\r"]
    path.write_text(json.dumps(data))
    with pytest.raises(reg.RegistryCorruptError):
        registry.verify(token, FP_A)
