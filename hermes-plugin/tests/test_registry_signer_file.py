# SPDX-License-Identifier: Apache-2.0
"""The read-only registry of the signer-published ``devices.json`` (T1.179; SAT-SC-06): owner, mode and file-type
checks, fail closed on every bad state, no cached copy, never opened for writing."""

from __future__ import annotations

import contextlib
import os
import pwd
import signal
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from signer_registry_helpers import FP, OTHER_FP, SIGNER_UID, make_public_dir, publish

from glasses import config as gcfg
from glasses.registry import RegistryPermissionError
from glasses.registry_parse import FILE_NAME, MAX_FILE_BYTES, RegistryError
from glasses.registry_signer_file import (
    _SIGNER_FORBIDDEN,
    ReadOnlyRegistryError,
    SignerFileRegistry,
    signer_file_registry,
)
from glasses.signer_client import NotConfigured, SignerUserRefused


def reader(tmp_path: Path, signer_uid: int = SIGNER_UID) -> SignerFileRegistry:
    return SignerFileRegistry(tmp_path / "public", signer_uid)


def test_a_valid_0640_file_of_the_signer_user_is_read(tmp_path):
    _, device, token = publish(tmp_path)
    found, tombstone = reader(tmp_path).lookup(token, FP)
    assert found == device and tombstone is False
    assert reader(tmp_path).lookup(token, FP)[0] == device
    assert [d.device_id for d in reader(tmp_path).list()] == [device.device_id]


def test_a_wrong_token_or_fingerprint_is_no_device_and_a_tombstone_is_reported(tmp_path):
    _, _, token = publish(tmp_path, revoke=True)
    assert reader(tmp_path).lookup("x" + token, FP) == (None, False)
    assert reader(tmp_path).lookup(token, "ef" * 32) == (None, False)
    assert reader(tmp_path).lookup(token, OTHER_FP) == (None, True)  # the signer's revoke: 403, not 401
    assert reader(tmp_path).lookup("x" * 43, OTHER_FP)[1] is True


def test_a_file_of_another_owner_is_refused(tmp_path, monkeypatch):
    _, _, token = publish(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: SIGNER_UID + 7)  # the plugin is not the owner of the directory
    with pytest.raises(RegistryError, match="owned by the signer user"):
        reader(tmp_path, SIGNER_UID + 1).lookup(token, FP)


@pytest.mark.parametrize("mode", [0o660, 0o620, 0o644, 0o641, 0o642, 0o646, 0o666, 0o740, 0o750, 0o1640])
def test_a_file_with_group_write_other_access_or_extra_bits_is_refused(tmp_path, mode):
    _, _, token = publish(tmp_path, mode=mode)
    assert stat.S_IMODE((tmp_path / "public" / FILE_NAME).stat().st_mode) == mode
    with pytest.raises(RegistryError, match="mode"):
        reader(tmp_path).lookup(token, FP)


def test_a_symlink_in_place_of_the_file_is_refused_also_when_it_points_at_a_valid_file(tmp_path):
    target, _, token = publish(tmp_path)
    real = tmp_path / "real.json"
    target.rename(real)
    target.symlink_to(real)
    with pytest.raises(RegistryError, match="cannot open safely"):
        reader(tmp_path).lookup(token, FP)


def test_a_symlinked_public_dir_is_refused_also_when_it_points_at_a_valid_directory(tmp_path):
    _, _, token = publish(tmp_path)
    (tmp_path / "public").rename(tmp_path / "real")
    (tmp_path / "public").symlink_to(tmp_path / "real")
    with pytest.raises(RegistryPermissionError, match="public dir"):
        reader(tmp_path).lookup(token, FP)


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777, 0o720, 0o702])
def test_a_public_dir_that_group_or_others_can_write_into_is_refused(tmp_path, mode):
    _, _, token = publish(tmp_path)
    (tmp_path / "public").chmod(mode)
    with pytest.raises(RegistryPermissionError, match="public dir"):
        reader(tmp_path).lookup(token, FP)


@pytest.mark.parametrize("mode", [0o750, 0o755, 0o700, 0o550])
def test_a_public_dir_without_group_or_other_write_still_serves(tmp_path, mode):
    _, device, token = publish(tmp_path)
    (tmp_path / "public").chmod(mode)
    try:
        assert reader(tmp_path).lookup(token, FP) == (device, False)
    finally:
        (tmp_path / "public").chmod(0o750)


def test_a_public_dir_owned_by_the_plugin_user_is_refused_when_that_user_is_not_the_signer(tmp_path):
    _, _, token = publish(tmp_path)
    with pytest.raises(RegistryPermissionError, match="public dir"):
        reader(tmp_path, SIGNER_UID + 1).lookup(token, FP)


def test_the_file_is_opened_relative_to_the_one_directory_that_was_checked(tmp_path, monkeypatch):
    _, _, token = publish(tmp_path)
    calls: list = []
    real_open = os.open

    def spy(path, flags, *args, dir_fd=None, **kwargs):
        calls.append((path, flags, dir_fd))
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    reader(tmp_path).lookup(token, FP)
    [(directory, flags, dir_fd)], [(name, _, name_dir_fd)] = (
        [c for c in calls if c[1] & os.O_DIRECTORY],
        [c for c in calls if not c[1] & os.O_DIRECTORY],
    )
    assert Path(directory) == tmp_path / "public" and flags & os.O_NOFOLLOW and dir_fd is None
    assert name == FILE_NAME and name_dir_fd is not None  # no walk over the full path


@contextlib.contextmanager
def deadline(seconds: int = 5):
    """Fail, not hang, when the body blocks (a FIFO opened without ``O_NONBLOCK`` waits for a writer)."""

    def expired(_signum, _frame):
        raise AssertionError(f"blocked for {seconds} s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_a_directory_and_a_fifo_in_place_of_the_file_are_refused_without_blocking(tmp_path):
    public = make_public_dir(tmp_path)
    (public / FILE_NAME).mkdir()
    with pytest.raises(RegistryError, match="not a regular file"):
        reader(tmp_path).lookup("t", FP)
    (public / FILE_NAME).rmdir()
    os.mkfifo(public / FILE_NAME, 0o640)
    with deadline(), pytest.raises(RegistryError, match="not a regular file"):
        reader(tmp_path).lookup("t", FP)


def test_a_missing_file_is_an_error_not_an_empty_registry(tmp_path):
    make_public_dir(tmp_path)
    with pytest.raises(RegistryError, match="missing"):
        reader(tmp_path).lookup("t", FP)
    with pytest.raises(RegistryError, match="missing"):
        reader(tmp_path).list()


def test_a_missing_public_dir_and_an_unreadable_file_are_errors(tmp_path):
    with pytest.raises(RegistryError):
        reader(tmp_path).list()
    target, _, _ = publish(tmp_path)
    target.chmod(0o000)
    try:
        if os.access(target, os.R_OK):
            pytest.skip("running with the right to read a 000 file")
        with pytest.raises(RegistryError, match="cannot open safely"):
            reader(tmp_path).list()
    finally:
        target.chmod(0o640)


@pytest.mark.parametrize("content", [b"", b"not json", b"[]", b'{"version": 2, "devices": []}', b'{"version":1}'])
def test_a_corrupt_file_is_an_error(tmp_path, content):
    target, _, _ = publish(tmp_path)
    target.write_bytes(content)
    with pytest.raises(RegistryError):
        reader(tmp_path).list()


def test_a_file_over_the_read_cap_is_an_error(tmp_path):
    target, _, _ = publish(tmp_path)
    with target.open("ab") as fh:
        fh.truncate(4 * 1024 * 1024 + 1)
    with pytest.raises(RegistryError, match="larger than"):
        reader(tmp_path).list()


def test_a_file_that_grows_after_the_size_check_is_refused(tmp_path, monkeypatch):
    target, _, _ = publish(tmp_path)
    with target.open("ab") as fh:
        fh.truncate(MAX_FILE_BYTES + 1)
    real_fstat = os.fstat

    def small(fd):  # the size the check saw, before the file grew
        st = real_fstat(fd)
        return SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid, st_size=100)

    monkeypatch.setattr(os, "fstat", small)
    with pytest.raises(RegistryError, match="larger than"):
        reader(tmp_path).list()


def test_the_forbidden_mask_has_the_setuid_and_setgid_bits():  # the sandbox refuses chmod with them
    assert _SIGNER_FORBIDDEN & 0o6000 == 0o6000


def test_no_earlier_copy_is_served_and_a_valid_file_restores_the_service(tmp_path):
    target, _, token = publish(tmp_path)
    registry = reader(tmp_path)
    assert registry.lookup(token, FP)[0] is not None
    good = target.read_bytes()
    target.write_bytes(b"garbage")
    with pytest.raises(RegistryError):
        registry.lookup(token, FP)  # the same instance: the earlier parse is not used
    target.unlink()
    with pytest.raises(RegistryError):
        registry.lookup(token, FP)
    target.write_bytes(good)
    target.chmod(0o640)
    assert registry.lookup(token, FP)[0] is not None


def test_the_file_is_never_opened_for_writing_and_add_and_revoke_are_refused(tmp_path, monkeypatch):
    target, device, token = publish(tmp_path)
    before = (target.read_bytes(), target.stat().st_mtime_ns, sorted(os.listdir(target.parent)))
    flags: list[int] = []
    real_open = os.open

    def spy(path, flag, *args, **kwargs):
        flags.append(flag)
        return real_open(path, flag, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    registry = reader(tmp_path)
    registry.lookup(token, FP)
    registry.list()
    with pytest.raises(ReadOnlyRegistryError):
        registry.add("phone", "ef" * 32, ["main"])
    with pytest.raises(ReadOnlyRegistryError):
        registry.revoke(device.device_id)
    assert flags and not any(f & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) for f in flags)
    assert (target.read_bytes(), target.stat().st_mtime_ns, sorted(os.listdir(target.parent))) == before


def fake_getpwnam(name: str) -> SimpleNamespace:  # one made-up account, so the test does not depend on the host
    if name != "signer-stand-in":
        raise KeyError(name)
    return SimpleNamespace(pw_uid=SIGNER_UID + 1)


def test_the_config_factory_reads_signer_public_dir_as_the_signer_user_and_refuses_what_is_unusable(
    tmp_path, monkeypatch
):
    publish(tmp_path)
    monkeypatch.setattr(pwd, "getpwnam", fake_getpwnam)
    cfg = gcfg.load({"signer": {"user": "signer-stand-in", "public_dir": str(tmp_path / "public")}})
    with pytest.raises(RegistryError, match="public dir"):  # the directory is ours
        signer_file_registry(cfg).list()
    with pytest.raises(NotConfigured):
        signer_file_registry(gcfg.load({"signer": {"user": "signer-stand-in"}}))
    with pytest.raises(SignerUserRefused):
        signer_file_registry(gcfg.load({"signer": {"user": "no-such-user-xyz", "public_dir": str(tmp_path)}}))
