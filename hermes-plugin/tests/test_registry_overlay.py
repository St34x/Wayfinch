# SPDX-License-Identifier: Apache-2.0
"""The plugin's deny overlay ``revoked-local.json`` (T1.179; SAT-SC-07, overlay half): written before a socket cut,
unioned with the signer's tombstones, narrow-only (no remove path), fail closed on an unsafe or corrupt file."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from signer_registry_helpers import FP, SIGNER_UID, publish

from glasses.registry_parse import InvalidDeviceError, RegistryError
from glasses.registry_signer_file import OVERLAY_NAME, DenyOverlay, SignerFileRegistry

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
OTHER_ID = "0123456789abcdef"
TIME = "2026-10-17T12:00:00Z"


def overlay_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / OVERLAY_NAME


def test_deny_writes_a_0600_file_and_a_new_instance_reads_it_back(tmp_path):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(OTHER_ID)
    path = overlay_file(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {"version": 1, "revoked": {OTHER_ID: "2026-10-17T12:00:00Z"}}
    assert DenyOverlay(tmp_path / "state").is_denied(OTHER_ID) and not overlay.is_denied("f" * 16)
    assert sorted(os.listdir(path.parent)) == [OVERLAY_NAME]  # no temp file left


def test_deny_is_idempotent_and_keeps_the_first_time(tmp_path):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(OTHER_ID)
    DenyOverlay(tmp_path / "state", clock=lambda: NOW.replace(year=2030)).deny(OTHER_ID)
    assert json.loads(overlay_file(tmp_path).read_text())["revoked"] == {OTHER_ID: "2026-10-17T12:00:00Z"}


def test_there_is_no_way_to_remove_an_entry(tmp_path):
    public = {n for n in dir(DenyOverlay) if not n.startswith("_")}
    assert public == {"deny", "is_denied", "denied_at", "apply"}  # apply only marks registry entries, in memory


@pytest.mark.parametrize("bad", ["", "x", "ABCDEF0123456789", OTHER_ID + "0", 7, None, b"0123456789abcdef"])
def test_a_device_id_of_the_wrong_shape_is_refused_and_writes_nothing(tmp_path, bad):
    overlay = DenyOverlay(tmp_path / "state")
    with pytest.raises(InvalidDeviceError):
        overlay.deny(bad)
    assert not overlay_file(tmp_path).exists()


def test_a_failed_write_still_denies_in_memory_and_raises(tmp_path):
    overlay = DenyOverlay(tmp_path / "missing" / "state")  # the parent does not exist
    with pytest.raises(RegistryError):
        overlay.deny(OTHER_ID)
    assert overlay.is_denied(OTHER_ID)  # narrow-only: the cut still happens


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"x",
        b"[]",
        b'{"version":2,"revoked":{}}',
        b'{"version":1}',
        b'{"version":1,"revoked":{"nope":"2026-10-17T12:00:00Z"}}',
        b'{"version":1,"revoked":{"0123456789abcdef":5}}',
        (f'{{"version":1,"revoked":{{"{OTHER_ID}":"{TIME}","{OTHER_ID}":"{TIME}"}}}}').encode(),  # a repeated key
    ],
)
def test_a_corrupt_overlay_is_an_error_at_load(tmp_path, content):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    overlay_file(tmp_path).write_bytes(content)
    overlay_file(tmp_path).chmod(0o600)
    with pytest.raises(RegistryError):
        DenyOverlay(state)


def test_an_unsafe_overlay_file_is_an_error_at_load(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = overlay_file(tmp_path)
    path.write_text('{"version":1,"revoked":{}}')
    path.chmod(0o640)
    with pytest.raises(RegistryError, match="mode"):
        DenyOverlay(state)
    path.chmod(0o600)
    real = tmp_path / "real.json"
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(RegistryError, match="cannot open safely"):
        DenyOverlay(state)


def test_a_device_in_the_overlay_is_revoked_although_the_signer_file_registry_says_active(tmp_path):
    _, device, token = publish(tmp_path)
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    registry = SignerFileRegistry(tmp_path / "public", SIGNER_UID, overlay)
    assert registry.lookup(token, FP)[0] == device
    overlay.deny(device.device_id)
    found, tombstone = registry.lookup(token, FP)
    assert found is None and tombstone is True  # the 403 marker, not a 401
    assert registry.lookup(token, FP)[0] is None and registry.lookup("x" * 43, FP)[1] is True
    assert [d.revoked_at for d in registry.list()] == ["2026-10-17T12:00:00Z"]


def test_the_overlay_and_the_signers_tombstones_are_a_union_and_an_unknown_id_changes_nothing(tmp_path):
    _, device, token = publish(tmp_path, revoke=True)
    overlay = DenyOverlay(tmp_path / "state")
    overlay.deny(OTHER_ID)  # not in the registry
    registry = SignerFileRegistry(tmp_path / "public", SIGNER_UID, overlay)
    assert registry.lookup(token, FP)[0] == device
    assert len(registry.list()) == 2 and sum(d.revoked_at is not None for d in registry.list()) == 1


def test_an_unreadable_published_file_still_fails_closed_with_an_overlay_entry(tmp_path):
    _, device, token = publish(tmp_path)
    overlay = DenyOverlay(tmp_path / "state")
    overlay.deny(device.device_id)
    (tmp_path / "public" / "devices.json").write_bytes(b"garbage")
    with pytest.raises(RegistryError):  # fail closed first: no device authenticates
        SignerFileRegistry(tmp_path / "public", SIGNER_UID, overlay).lookup(token, FP)
