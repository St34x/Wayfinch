# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the device registry (``glasses.registry``, T1.05; docs/architecture.md section 4)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from datetime import UTC, datetime

import pytest

from glasses import config as gcfg
from glasses import registry as reg
from glasses import registry_parse as rp

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


# -- add / lookup / list ---------------------------------------------------------------------


def test_add_then_verify_and_list(registry):
    device, token = registry.add("Phone", FP_A, ["assistant", "work"])
    assert len(token) >= 43  # 256 random bits, url-safe base64
    assert registry.lookup(token, FP_A)[0] == device
    assert registry.lookup(token, FP_A.upper())[0] == device
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
    assert registry.lookup(tok_a, FP_B)[0] is None  # token of A with the cert of B
    assert registry.lookup(tok_b, FP_B)[0] == b


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
    assert registry.lookup(token, fp)[0] is None


def test_verify_unknown_on_empty_registry(registry, state_dir):
    assert registry.lookup("anything", FP_A)[0] is None
    assert registry.list() == []
    assert not state_dir.exists()  # reads never create anything


def test_lookup_compares_every_entry_without_early_return(registry, monkeypatch):
    fps = (FP_A, FP_B, FP_C)
    issued = [registry.add(f"Phone {i}", fp, []) for i, fp in enumerate(fps)]
    seen: list[tuple[str, str]] = []

    def spy(a, b):
        seen.append((a, b))
        return a == b

    monkeypatch.setattr(reg.hmac, "compare_digest", spy)
    first, token = issued[0]
    assert registry.lookup(token, FP_A)[0] == first  # matches the FIRST entry: the loop must still run on
    assert len(seen) == 2 * len(fps)  # token + fingerprint for every entry
    assert {a for a, _ in seen} >= set(fps)
    assert (_sha(token), _sha(token)) in seen
    assert all(token not in pair for pair in seen)
    monkeypatch.undo()
    registry.revoke(first.device_id)
    monkeypatch.setattr(reg.hmac, "compare_digest", spy)
    seen.clear()
    assert registry.lookup(token, FP_A)[0] is None  # revoked entries are compared too, not skipped
    assert len(seen) == 2 * len(fps) + 1  # and the tombstone check of the one revoked entry
    assert {a for a, _ in seen} >= set(fps)


# -- revoke / tombstone ----------------------------------------------------------------------


def test_revoke_kills_token_and_keeps_fingerprint_tombstone(registry, state_dir):
    device, token = registry.add("Phone", FP_A, ["work"])
    revoked = registry.revoke(device.device_id)
    assert revoked.revoked_at == "2026-10-05T12:00:00Z"
    assert registry.lookup(token, FP_A)[0] is None
    assert registry.lookup("x" * 43, FP_A)[1] is True
    assert registry.lookup("x" * 43, FP_A.upper())[1] is True
    entry = json.loads(_file_text(state_dir))["devices"][0]
    assert entry["tokenSha256"] is None and entry["certFingerprint"] == FP_A
    assert _sha(token) not in _file_text(state_dir)
    assert registry.list() == [revoked]


def test_lookup_reports_no_tombstone_for_active_unknown_and_garbage(registry):
    registry.add("Phone", FP_A, [])
    for fp in (FP_A, FP_B, "nope", None):
        assert registry.lookup("x" * 43, fp)[1] is False


def test_lookup_answers_the_device_and_the_tombstone_in_one_read_for_every_kind_of_credential(registry):
    active, token = registry.add("Phone", FP_A, ["work"])
    gone, gone_token = registry.add("Old", FP_B, [])
    registry.revoke(gone.device_id)
    cases = [
        (token, FP_A, (active, False)),  # the active device
        (token, FP_A.upper(), (active, False)),
        (token, FP_B, (None, True)),  # its token with another device's (tombstoned) fingerprint
        ("x" * 43, FP_A, (None, False)),  # wrong token
        (gone_token, FP_B, (None, True)),  # a revoked device's old token
        (token, FP_C, (None, False)),  # unknown fingerprint
        (None, FP_A, (None, False)),
        (token, None, (None, False)),
        ("", "nope", (None, False)),
    ]
    for tok, fp, want in cases:
        assert registry.lookup(tok, fp) == want, (tok, fp)
    assert registry.lookup(token, FP_A) == (active, False)
    assert registry.lookup("x" * 43, FP_B) == (None, True)


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
    assert rp.MAX_LANES is gcfg.MAX_LANES
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


# -- no secrets in logs or errors ------------------------------------------------------------


def test_no_token_or_hash_in_logs_errors_or_repr(registry, state_dir, caplog):
    caplog.set_level(logging.DEBUG)
    device, token = registry.add("Phone", FP_A, ["work"])
    secrets_ = (token, _sha(token))
    registry.lookup(token, FP_A)[0]
    registry.lookup(token, FP_B)[0]
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
        registry.lookup(token, FP_A)[0]
    errors.append(info.value)
    (state_dir / reg.FILE_NAME).chmod(0o600)
    registry.revoke(device.device_id)
    texts = [caplog.text, repr(device), *(str(e) for e in errors), *(repr(e) for e in errors)]
    assert device.device_id in caplog.text  # events are logged, by id only
    assert not [s for s in secrets_ for text in texts if s in text]
