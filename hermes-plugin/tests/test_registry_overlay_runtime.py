# SPDX-License-Identifier: Apache-2.0
"""The deny overlay while the plugin runs (T1.179; SAT-SC-07, overlay half): the file corrupted, swapped or made
unwritable after start, many revocations at once, and what a failed write leaves behind."""

from __future__ import annotations

import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from signer_registry_helpers import FP, SIGNER_UID, publish

from glasses import registry_signer_file
from glasses.registry_parse import RegistryError
from glasses.registry_signer_file import OVERLAY_NAME, DenyOverlay, SignerFileRegistry

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
ID_A, ID_B, ID_C = "00000000000000aa", "00000000000000bb", "00000000000000cc"


def stored(tmp_path: Path) -> dict[str, str]:
    return dict(json.loads((tmp_path / "state" / OVERLAY_NAME).read_text())["revoked"])


def test_the_device_is_denied_in_memory_before_the_first_byte_is_written(tmp_path, monkeypatch):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    seen: list[bool] = []
    real = registry_signer_file._write_record

    def spy(*args: object) -> None:
        seen.append(overlay.is_denied(ID_A))
        real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_signer_file, "_write_record", spy)
    overlay.deny(ID_A)
    assert seen == [True]


def test_a_file_corrupted_while_running_does_not_lift_a_denial_and_the_next_write_repairs_it(tmp_path):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(ID_A)
    path = tmp_path / "state" / OVERLAY_NAME
    path.write_bytes(b"garbage")
    assert overlay.is_denied(ID_A)  # the memory is the authority while the plugin runs
    overlay.deny(ID_B)
    assert set(stored(tmp_path)) == {ID_A, ID_B}
    assert DenyOverlay(tmp_path / "state").is_denied(ID_A)


def test_a_file_emptied_while_running_is_an_error_at_the_next_start_not_an_empty_overlay(tmp_path):
    overlay = DenyOverlay(tmp_path / "state")
    overlay.deny(ID_A)
    (tmp_path / "state" / OVERLAY_NAME).write_bytes(b"")
    with pytest.raises(RegistryError):
        DenyOverlay(tmp_path / "state")


def test_a_symlink_put_in_place_of_the_file_is_replaced_and_its_target_is_never_written(tmp_path):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(ID_A)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    path = tmp_path / "state" / OVERLAY_NAME
    path.unlink()
    path.symlink_to(victim)
    overlay.deny(ID_B)
    assert victim.read_text() == "precious"
    assert not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(stored(tmp_path)) == {ID_A, ID_B}


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o707])
def test_a_state_dir_opened_up_after_start_refuses_the_write_but_the_denial_holds(tmp_path, mode):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(ID_A)
    (tmp_path / "state").chmod(mode)
    try:
        with pytest.raises(RegistryError, match="state dir"):
            overlay.deny(ID_B)
        assert overlay.is_denied(ID_B) and overlay.is_denied(ID_A)
        assert set(stored(tmp_path)) == {ID_A}
    finally:
        (tmp_path / "state").chmod(0o700)


def test_a_read_only_state_dir_raises_keeps_the_denial_and_leaves_no_temp_file(tmp_path):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(ID_A)
    (tmp_path / "state").chmod(0o500)
    try:
        if os.access(tmp_path / "state", os.W_OK):
            pytest.skip("running with the right to write into a read-only directory")
        with pytest.raises(RegistryError):
            overlay.deny(ID_B)
        assert overlay.is_denied(ID_B)
        assert sorted(os.listdir(tmp_path / "state")) == [OVERLAY_NAME]
    finally:
        (tmp_path / "state").chmod(0o700)


def test_a_failed_write_still_denies_the_device_on_the_signer_file_registry(tmp_path):
    _, device, token = publish(tmp_path)
    overlay = DenyOverlay(tmp_path / "missing" / "state")  # the parent does not exist: every write fails
    registry = SignerFileRegistry(tmp_path / "public", SIGNER_UID, overlay)
    assert registry.lookup(token, FP)[0] == device
    with pytest.raises(RegistryError):
        overlay.deny(device.device_id)
    assert registry.lookup(token, FP) == (None, True)  # the 403 marker although nothing reached the disk


def test_a_repeated_deny_after_a_failed_write_persists_it_or_raises_again(tmp_path):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(ID_A)
    (tmp_path / "state").chmod(0o500)
    try:
        with pytest.raises(RegistryError):
            overlay.deny(ID_B)
    finally:
        (tmp_path / "state").chmod(0o700)
    try:
        overlay.deny(ID_B)  # the operator retries once the disk is writable again
    except RegistryError:
        return
    assert ID_B in stored(tmp_path)  # either an error again or the entry on disk; silence with no entry is the gap


def test_the_retry_of_a_denial_whose_write_failed_puts_it_on_disk_and_later_denials_stay_cheap(tmp_path):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(ID_A)
    (tmp_path / "state").chmod(0o500)
    try:
        with pytest.raises(RegistryError):
            overlay.deny(ID_B)
        assert overlay.is_denied(ID_B)  # denied in memory although nothing reached the disk
    finally:
        (tmp_path / "state").chmod(0o700)
    overlay.deny(ID_B)
    assert {ID_A, ID_B} <= set(stored(tmp_path))
    before = (tmp_path / "state" / "revoked-local.json").stat().st_mtime_ns
    overlay.deny(ID_B)  # persisted now: no further write
    assert (tmp_path / "state" / "revoked-local.json").stat().st_mtime_ns == before


def test_many_revocations_at_once_are_all_kept_and_the_file_is_always_whole(tmp_path):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    ids = [f"{i:016x}" for i in range(24)]
    errors: list[BaseException] = []
    stop = threading.Event()

    def revoke(device_id: str) -> None:
        try:
            overlay.deny(device_id)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted empty below
            errors.append(exc)

    def read_back() -> None:
        while not stop.is_set():
            try:
                DenyOverlay(tmp_path / "state")  # a start-up reading the file in the middle of the writes
            except RegistryError as exc:
                errors.append(exc)

    (tmp_path / "state").mkdir(mode=0o700)
    reader = threading.Thread(target=read_back)
    reader.start()
    threads = [threading.Thread(target=revoke, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    stop.set()
    reader.join(10)
    assert not errors
    assert set(stored(tmp_path)) == set(ids)
    assert all(overlay.is_denied(i) for i in ids)
    assert sorted(os.listdir(tmp_path / "state")) == [OVERLAY_NAME]


def test_apply_keeps_the_signers_own_revocation_time_and_drops_the_token_hash_of_a_denied_entry():
    overlay = DenyOverlay.__new__(DenyOverlay)
    overlay._denied = {ID_A: "2026-10-17T12:00:00Z", ID_B: "2026-10-17T12:00:00Z"}
    entries = [
        {"deviceId": ID_A, "revokedAt": None, "tokenSha256": "x" * 64},
        {"deviceId": ID_B, "revokedAt": "2026-01-01T00:00:00Z", "tokenSha256": None},
        {"deviceId": ID_C, "revokedAt": None, "tokenSha256": "y" * 64},
    ]
    overlay.apply(entries)
    assert entries[0] == {"deviceId": ID_A, "revokedAt": "2026-10-17T12:00:00Z", "tokenSha256": None}
    assert entries[1]["revokedAt"] == "2026-01-01T00:00:00Z"
    assert entries[2]["revokedAt"] is None


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_a_fifo_or_a_directory_in_place_of_the_overlay_is_an_error_at_start_and_does_not_hang(tmp_path, kind):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    if kind == "fifo":
        os.mkfifo(state / OVERLAY_NAME, 0o600)
    else:
        (state / OVERLAY_NAME).mkdir(mode=0o700)
    outcome: list[object] = []

    def start() -> None:
        try:
            DenyOverlay(state)
        except RegistryError as exc:
            outcome.append(exc)

    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and len(outcome) == 1


def test_an_overlay_file_over_the_read_cap_is_an_error_at_start(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / OVERLAY_NAME
    path.write_bytes(b"{" + b" " * (4 * 1024 * 1024 + 1))
    path.chmod(0o600)
    with pytest.raises(RegistryError, match="larger than"):
        DenyOverlay(state)
