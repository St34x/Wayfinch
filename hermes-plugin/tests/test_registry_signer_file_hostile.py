# SPDX-License-Identifier: Apache-2.0
"""The signer's ``devices.json`` under attack (T1.179; SAT-SC-06): the file replaced between the checks and the read,
a signer rewrite caught half-way, odd or huge JSON, the setuid/setgid mode bits and the directory around the file."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import pwd
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from signer_registry_helpers import FP, OTHER_FP, SIGNER_UID, publish

from glasses import config as gcfg
from glasses.registry_parse import FILE_NAME, MAX_FILE_BYTES, RegistryError
from glasses.registry_signer_file import SignerFileRegistry, signer_file_registry
from glasses.signer_client import NotConfigured, SignerUserRefused


def reader(tmp_path: Path) -> SignerFileRegistry:
    return SignerFileRegistry(tmp_path / "public", SIGNER_UID)


def opens_the_file(path: Any, kwargs: dict[str, Any], target: Path) -> bool:
    """An ``os.open`` of the registry file: by full path, or by its name relative to the opened directory."""
    name = os.fspath(path)
    return name == str(target) or (name == target.name and kwargs.get("dir_fd") is not None)


def replace_atomically(target: Path, data: bytes, mode: int = 0o640) -> None:
    """What the signer does: write a sibling, then rename it over the file."""
    tmp = target.with_name(target.name + ".new")
    tmp.write_bytes(data)
    tmp.chmod(mode)
    os.replace(tmp, target)


def test_the_checks_and_the_read_use_the_opened_file_never_its_path(tmp_path, monkeypatch):
    target, device, token = publish(tmp_path)
    named: list[str] = []
    real_stat, real_lstat = os.stat, os.lstat

    def spy(real: Callable[..., Any]) -> Callable[..., Any]:
        def inner(path: Any, *args: Any, **kwargs: Any) -> Any:
            named.append(os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else "<fd>")
            return real(path, *args, **kwargs)

        return inner

    monkeypatch.setattr(os, "stat", spy(real_stat))
    monkeypatch.setattr(os, "lstat", spy(real_lstat))
    assert reader(tmp_path).lookup(token, FP)[0] == device
    assert not [p for p in named if FILE_NAME in os.fsdecode(p)]  # a path-based check could be raced; fstat cannot


@pytest.mark.parametrize("swap", ["symlink", "garbage", "loose_mode", "fifo", "removed"])
def test_a_file_swapped_after_the_open_changes_nothing_the_opened_inode_is_what_is_judged(tmp_path, monkeypatch, swap):
    target, device, token = publish(tmp_path)
    real_open, done = os.open, []

    def swap_after_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if opens_the_file(path, kwargs, target) and not done:
            done.append(True)
            target.unlink()
            if swap == "symlink":
                target.symlink_to(tmp_path / "elsewhere")
            elif swap == "garbage":
                target.write_bytes(b"garbage")
            elif swap == "loose_mode":
                target.write_bytes(b"{}")
                target.chmod(0o666)
            elif swap == "fifo":
                os.mkfifo(target, 0o640)
        return fd

    monkeypatch.setattr(os, "open", swap_after_open)
    assert reader(tmp_path).lookup(token, FP)[0] == device  # the inode that was opened was valid
    assert done


def test_a_file_whose_mode_is_widened_between_open_and_fstat_is_refused(tmp_path, monkeypatch):
    target, _, token = publish(tmp_path)
    real_open = os.open

    def widen_after_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if opens_the_file(path, kwargs, target):
            target.chmod(0o666)  # the same inode: fstat sees the mode it has now
        return fd

    monkeypatch.setattr(os, "open", widen_after_open)
    with pytest.raises(RegistryError, match="mode"):
        reader(tmp_path).lookup(token, FP)


def test_a_file_that_grows_after_the_size_check_is_cut_off_at_the_cap(tmp_path, monkeypatch):
    target, _, token = publish(tmp_path)
    with target.open("ab") as fh:
        fh.write(b" " * (MAX_FILE_BYTES + 10))
    real_fstat = os.fstat

    def small(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result((*tuple(st)[:6], 100, *tuple(st)[7:]))  # st_size lies: the file looked small

    monkeypatch.setattr(os, "fstat", small)
    with pytest.raises(RegistryError, match="larger than"):
        reader(tmp_path).lookup(token, FP)


def test_a_file_of_exactly_the_cap_is_read_and_one_byte_more_is_not(tmp_path):
    target, device, token = publish(tmp_path)
    body = target.read_bytes().rstrip(b"\n")
    replace_atomically(target, body + b" " * (MAX_FILE_BYTES - len(body)))
    assert target.stat().st_size == MAX_FILE_BYTES
    assert reader(tmp_path).lookup(token, FP)[0] == device
    replace_atomically(target, body + b" " * (MAX_FILE_BYTES + 1 - len(body)))
    with pytest.raises(RegistryError, match="larger than"):
        reader(tmp_path).lookup(token, FP)


def test_every_prefix_of_a_file_caught_mid_write_is_an_error_never_a_registry(tmp_path):
    target, _, token = publish(tmp_path, revoke=True)
    good = target.read_bytes()
    registry = reader(tmp_path)
    for size in range(len(good) - 1):  # good[:-1] only lacks the final newline and is valid JSON
        target.write_bytes(good[:size])
        with pytest.raises(RegistryError):
            registry.lookup(token, FP)
    target.write_bytes(good)
    assert registry.lookup(token, FP)[0] is not None


def test_readers_during_atomic_signer_rewrites_see_only_whole_files(tmp_path):
    target, device, token = publish(tmp_path)
    active = target.read_bytes()
    revoked_text = json.loads(active)
    revoked_text["devices"][0].update(revokedAt="2026-10-17T12:00:00Z", tokenSha256=None)
    revoked = json.dumps(revoked_text, indent=2, sort_keys=True).encode() + b"\n"
    stop, bad, seen = threading.Event(), [], set()
    registry = reader(tmp_path)

    def read() -> None:
        while not stop.is_set():
            try:
                found, tombstone = registry.lookup(token, FP)
            except RegistryError as exc:
                bad.append(exc)
                continue
            seen.add((found == device, tombstone))

    threads = [threading.Thread(target=read) for _ in range(6)]
    for t in threads:
        t.start()
    for i in range(300):
        replace_atomically(target, revoked if i % 2 else active)
    stop.set()
    for t in threads:
        t.join(10)
    assert not bad  # an atomic rename is never seen half-done
    assert seen <= {(True, False), (False, True)}  # active, or revoked with the tombstone: nothing in between


def test_readers_during_an_in_place_rewrite_get_a_whole_registry_or_an_error_never_another_answer(tmp_path):
    target, device, token = publish(tmp_path)
    good = target.read_bytes()
    stop, answers = threading.Event(), set()
    registry = reader(tmp_path)

    def read() -> None:
        while not stop.is_set():
            try:
                found, tombstone = registry.lookup(token, FP)
                answers.add(("device" if found == device else "other", tombstone))
            except RegistryError:
                answers.add(("error", False))

    threads = [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(300):
        with target.open("r+b") as fh:  # not what the signer does: truncate, then write
            fh.truncate(0)
            fh.write(good)
    stop.set()
    for t in threads:
        t.join(10)
    assert answers <= {("device", False), ("error", False)}


def doc_with(good: bytes, old: bytes, new: bytes) -> bytes:
    assert good.count(old) == 1
    return good.replace(old, new)


def odd_documents(good: bytes) -> dict[str, bytes]:
    return {
        "repeated top-level key": b'{"version":1,"devices":[],"devices":[]}',
        "repeated entry key": doc_with(good, b'"name": "glasses"', b'"name": "glasses", "name": "other"'),
        "NaN in a field": doc_with(good, b'"lastSeen": null', b'"lastSeen": NaN'),
        "Infinity version": doc_with(good, b'"version": 1', b'"version": Infinity'),
        "float version": doc_with(good, b'"version": 1', b'"version": 1.0'),
        "bool version": doc_with(good, b'"version": 1', b'"version": true'),
        "huge integer": doc_with(good, b'"version": 1', b'"version": ' + b"9" * 6000),
        "very deep nesting": b"[" * 200_000 + b"]" * 200_000,
        "deep nesting in an entry": doc_with(good, b'"features": []', b'"features": ' + b"[" * 50_000 + b"]" * 50_000),
        "invalid UTF-8": doc_with(good, b'"glasses"', b'"gl\xffsses"'),
        "lone surrogate": doc_with(good, b'"glasses"', b'"gl\\ud800sses"'),
        "NUL in the name": doc_with(good, b'"glasses"', b'"gl\\u0000sses"'),
        "trailing garbage": good + b"x",
        "two documents": good + good,
        "a second device with the same id": b"",  # filled in below
    }


def test_odd_documents_are_an_error_or_a_correct_registry_never_another_exception(tmp_path):
    target, device, token = publish(tmp_path)
    good = target.read_bytes()
    docs = odd_documents(good)
    parsed = json.loads(good)
    parsed["devices"].append(dict(parsed["devices"][0]))
    docs["a second device with the same id"] = json.dumps(parsed).encode()
    docs["utf-16"] = good.decode().encode("utf-16")
    docs["utf-8 byte order mark"] = b"\xef\xbb\xbf" + good
    for label, content in docs.items():
        target.write_bytes(content)
        try:
            found = reader(tmp_path).lookup(token, FP)[0]
        except RegistryError:
            continue
        # accepted: only when it is the very same registry (an encoding or BOM variant), never a changed one
        assert label in {"utf-16", "utf-8 byte order mark"} and found == device, label


def test_a_registry_of_thousands_of_entries_near_the_cap_finds_the_last_device(tmp_path):
    target, _, _ = publish(tmp_path)
    doc = json.loads(target.read_bytes())
    template = doc["devices"][0]
    entries = []
    for i in range(8000):
        entry = dict(template, deviceId=f"{i:016x}", certFingerprint=f"{i:064x}", name=f"device {i}")
        entries.append(entry)
    entries[-1] = dict(template, deviceId="ffffffffffffffff", certFingerprint=OTHER_FP)
    doc["devices"] = entries
    target.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    target.chmod(0o640)
    assert MAX_FILE_BYTES // 2 < target.stat().st_size <= MAX_FILE_BYTES
    assert len(reader(tmp_path).list()) == 8000
    assert reader(tmp_path).lookup("t" * 43, OTHER_FP) == (None, False)


@pytest.mark.parametrize("mode", [0o4640, 0o2640, 0o6640, 0o4440])
def test_a_setuid_or_setgid_file_is_refused(tmp_path, mode):
    target, _, token = publish(tmp_path)
    try:
        target.chmod(mode)
    except PermissionError:
        pytest.skip("this sandbox refuses chmod with setuid/setgid (project KB sandbox-chmod-setuid-eperm)")
    if stat.S_IMODE(target.stat().st_mode) != mode:
        pytest.skip("the kernel dropped the setgid bit (not a member of the file's group)")
    with pytest.raises(RegistryError, match="mode"):
        reader(tmp_path).lookup(token, FP)


@pytest.mark.parametrize("mode", [0o770, 0o775, 0o777, 0o757])
def test_a_public_dir_that_group_or_others_can_write_into_is_refused(tmp_path, mode):
    _, _, token = publish(tmp_path)
    public = tmp_path / "public"
    public.chmod(mode)
    try:
        with pytest.raises(RegistryError):
            reader(tmp_path).lookup(token, FP)
    finally:
        public.chmod(0o750)


def test_a_stale_signer_copy_renamed_over_the_file_is_served_as_nothing_detects_a_rollback(tmp_path):
    """Documents a limit: whoever may write the directory (the signer) can roll the file back to an older valid one."""
    target, device, token = publish(tmp_path)
    old = target.read_bytes()
    revoked = json.loads(old)
    revoked["devices"][0].update(revokedAt="2026-10-17T12:00:00Z", tokenSha256=None)
    replace_atomically(target, json.dumps(revoked, indent=2, sort_keys=True).encode() + b"\n")
    assert reader(tmp_path).lookup(token, FP) == (None, True)
    replace_atomically(target, old)  # rename of an old inode, which is what a hard link kept alive would give
    assert reader(tmp_path).lookup(token, FP)[0] == device


def test_the_inherited_private_writers_cannot_reach_the_signers_directory(tmp_path):
    target, _, _ = publish(tmp_path)
    before = target.read_bytes()
    registry = reader(tmp_path)
    with contextlib.suppress(RegistryError):
        registry._write([], None)  # a refactor that calls the base writer must not write
    assert target.read_bytes() == before
    assert sorted(os.listdir(target.parent)) == ["device-ca.pem", FILE_NAME]


def test_nothing_in_the_public_dir_is_touched_by_any_read_when_the_dir_is_read_only(tmp_path):
    target, _, token = publish(tmp_path)
    public = tmp_path / "public"
    before = (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino, public.stat().st_mtime_ns)
    public.chmod(0o500)
    target.chmod(0o440)
    try:
        registry = reader(tmp_path)
        for _ in range(3):
            registry.lookup(token, FP)
            registry.list()
            registry.lookup("x" * 43, OTHER_FP)[1]
        assert sorted(os.listdir(public)) == ["device-ca.pem", FILE_NAME]
    finally:
        public.chmod(0o750)
        target.chmod(0o640)
    assert (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino, public.stat().st_mtime_ns) == before


def test_a_missing_public_dir_is_an_error_and_is_not_created(tmp_path):
    with pytest.raises(RegistryError):
        SignerFileRegistry(tmp_path / "nowhere" / "public", SIGNER_UID).list()
    assert not (tmp_path / "nowhere").exists()


@pytest.mark.parametrize("user", ["root", pwd.getpwuid(os.geteuid()).pw_name, "no-such-user-xyz", ""])
def test_the_factory_refuses_root_the_plugins_own_user_an_unknown_and_an_empty_signer_user(tmp_path, user):
    publish(tmp_path)
    cfg = gcfg.load({"signer": {"user": user, "public_dir": str(tmp_path / "public")}})
    with pytest.raises((SignerUserRefused, NotConfigured)):
        signer_file_registry(cfg)  # at construction: no reader exists that could accept the plugin's own file


def test_an_io_error_during_the_read_is_a_typed_registry_error(tmp_path, monkeypatch):
    _, _, token = publish(tmp_path)
    real_fdopen = os.fdopen

    class Failing:
        def __init__(self, fd: int) -> None:
            self.fh = real_fdopen(fd, "rb")

        def __enter__(self) -> Failing:
            return self

        def __exit__(self, *exc: object) -> None:
            self.fh.close()

        def read(self, size: int = -1) -> bytes:
            raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fdopen", lambda fd, mode="r": Failing(fd))
    with pytest.raises(RegistryError):
        reader(tmp_path).lookup(token, FP)


@pytest.mark.parametrize("bit", [stat.S_ISUID, stat.S_ISGID, stat.S_ISVTX, stat.S_ISUID | stat.S_ISGID])
def test_a_special_mode_bit_is_refused_although_the_sandbox_cannot_chmod_it(tmp_path, monkeypatch, bit):
    """The mode comes from a faked ``fstat`` so the setuid/setgid part of the mask is tested where ``chmod`` is not."""
    _, _, token = publish(tmp_path)
    real_fstat = os.fstat

    def with_bit(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result((st.st_mode | bit, *tuple(st)[1:]))

    monkeypatch.setattr(os, "fstat", with_bit)
    with pytest.raises(RegistryError, match="mode"):
        reader(tmp_path).lookup(token, FP)


@pytest.mark.parametrize("mode", [0o650, 0o610, 0o601, 0o604])
def test_an_execute_bit_of_the_group_or_others_or_read_for_others_alone_is_refused(tmp_path, mode):
    _, _, token = publish(tmp_path)
    (tmp_path / "public" / FILE_NAME).chmod(mode)
    with pytest.raises(RegistryError, match="mode"):
        reader(tmp_path).lookup(token, FP)
