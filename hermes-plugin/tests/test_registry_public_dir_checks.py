# SPDX-License-Identifier: Apache-2.0
"""The directory around the signer's ``devices.json`` (T1.179): opened once with ``O_NOFOLLOW`` and checked on the
open descriptor, the file read relative to it, the descriptor closed on every path, and no path or token in an error."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from signer_registry_helpers import FP, SIGNER_UID, publish

from glasses.registry_parse import FILE_NAME, MAX_FILE_BYTES, RegistryError
from glasses.registry_signer_file import ReadOnlyRegistryError, SignerFileRegistry


def reader(tmp_path: Path, uid: int = SIGNER_UID) -> SignerFileRegistry:
    return SignerFileRegistry(tmp_path / "public", uid)


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_the_file_is_opened_relative_to_the_directory_descriptor_that_was_checked(tmp_path, monkeypatch):
    _, device, token = publish(tmp_path)
    real_open, opens, returned = os.open, [], []

    def spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        opens.append((os.fspath(path), flags, kwargs.get("dir_fd")))
        returned.append(fd)
        return fd

    monkeypatch.setattr(os, "open", spy)
    assert reader(tmp_path).lookup(token, FP)[0] == device
    (dir_path, dir_flags, dir_dir_fd), (file_path, file_flags, file_dir_fd) = opens
    assert dir_path == str(tmp_path / "public") and dir_dir_fd is None
    assert dir_flags & os.O_DIRECTORY and dir_flags & os.O_NOFOLLOW
    assert file_path == FILE_NAME and file_dir_fd == returned[0]  # a bare name, relative to the checked descriptor
    assert file_flags & os.O_NOFOLLOW and not file_flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)


def break_the_file(tmp_path: Path, how: str) -> None:
    target = tmp_path / "public" / FILE_NAME
    target.unlink()
    if how == "garbage":
        target.write_bytes(b"garbage")
    elif how == "symlink":
        target.symlink_to(tmp_path / "elsewhere")
    elif how == "loose_mode":
        shutil.copyfile(tmp_path / "source" / FILE_NAME, target)
        target.chmod(0o666)
    elif how == "oversize":
        target.write_bytes(b" " * (MAX_FILE_BYTES + 1))
        target.chmod(0o640)
    elif how == "fifo":
        os.mkfifo(target, 0o640)
    elif how == "directory":
        target.mkdir()


@pytest.mark.parametrize("how", ["ok", "missing", "garbage", "symlink", "loose_mode", "oversize", "fifo", "directory"])
def test_no_descriptor_is_left_open_after_a_read_whatever_the_file_is(tmp_path, how):
    _, _, token = publish(tmp_path)
    if how == "missing":
        (tmp_path / "public" / FILE_NAME).unlink()
    elif how != "ok":
        break_the_file(tmp_path, how)
    registry, before = reader(tmp_path), open_fds()
    for _ in range(5):
        try:
            registry.lookup(token, FP)
        except RegistryError:
            assert how != "ok"
    assert open_fds() == before


@pytest.mark.parametrize("failing_call", [1, 2])  # 1: the fstat of the directory, 2: the fstat of the file
def test_no_descriptor_is_left_open_when_an_fstat_raises(tmp_path, monkeypatch, failing_call):
    _, _, token = publish(tmp_path)
    real_fstat, calls = os.fstat, []

    def flaky(fd: int) -> os.stat_result:
        calls.append(fd)
        if len(calls) == failing_call:
            raise OSError(errno.EIO, "Input/output error")
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", flaky)
    registry, before = reader(tmp_path), open_fds()
    with pytest.raises((OSError, RegistryError)):  # an fstat failure is still a bare OSError; only the read is typed
        registry.lookup(token, FP)
    assert open_fds() == before


def test_no_descriptor_is_left_open_when_the_directory_is_refused(tmp_path):
    _, _, token = publish(tmp_path)
    (tmp_path / "public").chmod(0o770)
    registry, before = reader(tmp_path), open_fds()
    try:
        for _ in range(3):
            with pytest.raises(RegistryError):
                registry.lookup(token, FP)
    finally:
        (tmp_path / "public").chmod(0o750)
    assert open_fds() == before


def test_a_directory_replaced_by_a_symlink_between_two_reads_is_refused_at_the_second(tmp_path):
    _, device, token = publish(tmp_path)
    registry = reader(tmp_path)
    assert registry.lookup(token, FP)[0] == device
    (tmp_path / "public").rename(tmp_path / "real-public")
    (tmp_path / "public").symlink_to(tmp_path / "real-public", target_is_directory=True)
    with pytest.raises(RegistryError, match="public dir"):  # the old, valid copy behind the link is not served
        registry.lookup(token, FP)
    with pytest.raises(RegistryError, match="public dir"):
        registry.list()


@pytest.mark.parametrize("mode", [0o020, 0o002, 0o720, 0o702, 0o722])
def test_a_directory_with_a_group_or_other_write_bit_alone_is_refused(tmp_path, mode):
    _, _, token = publish(tmp_path)
    public = tmp_path / "public"
    public.chmod(mode)  # 0o020 and 0o002 also lack owner access: refused at the open, still refused
    try:
        with pytest.raises(RegistryError):
            reader(tmp_path).lookup(token, FP)
    finally:
        public.chmod(0o750)


@pytest.mark.parametrize("mode", [0o750, 0o700, 0o755, 0o710])
def test_a_directory_of_the_signer_that_group_and_others_cannot_write_is_served(tmp_path, mode):
    _, device, token = publish(tmp_path)
    (tmp_path / "public").chmod(mode)
    assert reader(tmp_path).lookup(token, FP)[0] == device


def test_a_directory_owned_by_the_plugin_user_is_refused_when_the_signer_is_someone_else(tmp_path):
    _, _, token = publish(tmp_path)
    with pytest.raises(RegistryError, match="public dir"):  # the directory check, not the file's owner check
        reader(tmp_path, SIGNER_UID + 1).lookup(token, FP)


@pytest.mark.parametrize(
    "fault", ["missing_file", "symlinked_dir", "wide_dir", "wrong_dir_owner", "wide_file", "garbage"]
)
def test_no_error_text_holds_a_path_or_the_token(tmp_path, fault):
    _, _, token = publish(tmp_path)
    public, uid = tmp_path / "public", SIGNER_UID
    if fault == "missing_file":
        (public / FILE_NAME).unlink()
    elif fault == "symlinked_dir":
        public.rename(tmp_path / "real")
        public.symlink_to(tmp_path / "real", target_is_directory=True)
    elif fault == "wide_dir":
        public.chmod(0o777)
    elif fault == "wrong_dir_owner":
        uid += 1
    elif fault == "wide_file":
        (public / FILE_NAME).chmod(0o666)
    else:
        break_the_file(tmp_path, "garbage")
    try:
        with pytest.raises(RegistryError) as info:
            reader(tmp_path, uid).lookup(token, FP)
    finally:
        if public.is_dir() and not public.is_symlink():
            public.chmod(0o750)
    text = f"{info.value} {info.value!r} {info.value.args}"
    assert str(tmp_path) not in text and token not in text and "/" not in str(info.value)


WRITER_CALLS = [
    ("add", ("bad name", "x", ["no-such-lane"]), {}),
    ("add", (), {}),
    ("add", ("phone",), {"fingerprint": "nope", "lanes": []}),
    ("add", (), {"name": "bad name", "fingerprint": "nope", "lanes": ["x"], "unknown": 1}),
    ("revoke", (), {"device_id": "not an id"}),
    ("revoke", ("a", "b", "c"), {}),
    ("_write", (), {"entries": [], "max_bytes": 1}),
    ("_write", ([], 1, 2, 3), {}),
    ("_transaction", (), {"max_bytes": -5}),
    ("_open_lock", (1, 2), {"x": 1}),
    ("_sweep_temps", ("x",), {"y": 2}),
]


@pytest.mark.parametrize(("name", "args", "kwargs"), WRITER_CALLS)
def test_every_writer_raises_read_only_whatever_the_arguments(tmp_path, name, args, kwargs):
    target, _, _ = publish(tmp_path)
    before = (target.read_bytes(), sorted(os.listdir(target.parent)), open_fds())
    with pytest.raises(ReadOnlyRegistryError):  # not TypeError for the arity, not InvalidDeviceError for the value
        getattr(reader(tmp_path), name)(*args, **kwargs)
    assert (target.read_bytes(), sorted(os.listdir(target.parent)), open_fds()) == before
