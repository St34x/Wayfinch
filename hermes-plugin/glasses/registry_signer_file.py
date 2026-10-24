# SPDX-License-Identifier: Apache-2.0
"""The signer's ``devices.json`` read-only, the plugin's deny overlay and its last-seen file (T1.179; signer protocol).

``SignerFileRegistry`` is the ``Registry`` reading ``signer.public_dir``/``devices.json``, which only the device-CA
signer writes. Same format v1, same ``lookup``/``list``; every writer of ``Registry`` raises
``ReadOnlyRegistryError`` first and the file is never opened for writing. Fail closed, with a typed ``RegistryError``
(the upgrade answers ``401`` for every device): the directory is opened once with ``O_NOFOLLOW`` and refused when it
is a symlink, owned by the plugin's own user or writable by group or others; the file, opened relative to it, when it
is missing, a symlink or non-regular, owned by anyone but the signer user, of a mode with a bit beyond ``0640``
(``0640`` or stricter is read), oversize or corrupt. A symlink is refused for the last path component only: the
parents of ``signer.public_dir`` are the operator's job (root- or signer-owned, not writable by the plugin user). It
parses on every call and keeps no copy, so an earlier parse is never served and the next valid file restores service.

``DenyOverlay`` is ``revoked-local.json`` in the state dir (0600): ``deny(deviceId)`` records a revocation of the
plugin's own, loaded at start and unioned into every read of the registry (a device in it reads as revoked, the ``403``
marker, whatever the signer's file says). It has no remove path and a failed write still denies in memory.
``is_denied`` is the in-memory answer for the injected ``is_revoked``. A corrupt or unsafe overlay raises at
construction. Residual: the file is a convenience copy held by the plugin's own user; a process with that uid can drop
an entry (effective at the next gateway restart) or add one (that phone is told it was revoked and wipes itself); real
revocations go through the signer, whose tombstone that user cannot edit.

``LastSeen`` is ``last-seen.json`` (0600, display only): ``touch(device)`` is the ``on_authenticated`` hook. It never
blocks the loop (one writer thread), writes a device at most once a minute (by the monotonic clock) and never touches
``devices.json``; a corrupt file is dropped with a warning, a failed write is logged by type. Call ``close()`` on
shutdown, off the loop, to flush.

Not here: the 1 s mtime watcher that cuts live sessions and raises the alert when the file turns bad (T1.63a,
``revoke_watch``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

from .config_core import GlassesConfig
from .registry import Device, Registry, RegistryPermissionError
from .registry_parse import (
    FILE_NAME,
    FORMAT_VERSION,
    ID_RE,
    MAX_FILE_BYTES,
    InvalidDeviceError,
    RegistryCorruptError,
    RegistryError,
    parse,
)
from .signer_client import public_file, signer_user_uid
from .signer_protocol import PUBLIC_REGISTRY

_LOG = logging.getLogger(__name__)

OVERLAY_NAME = "revoked-local.json"
LAST_SEEN_NAME = "last-seen.json"
MIN_INTERVAL_S = 60  # a device is written to last-seen.json at most this often
_TIME_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_SIGNER_FORBIDDEN = 0o7137  # every mode bit beyond rw-r----- (group write, others, exec, setuid, setgid, sticky)
_OWNER_FORBIDDEN = 0o7177  # every mode bit beyond rw-------


class ReadOnlyRegistryError(RegistryError):
    """A write was asked of the signer's registry file, which only the signer writes."""


class RegistryMissingError(RegistryError):
    """The signer has not published ``devices.json`` (or it was removed)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Rule(NamedTuple):
    """Who must own a file, which mode bits it must not have, and how the refusal names that owner."""

    uid: int
    forbidden: int
    who: str


def _checked_fd(path: os.PathLike[str] | str, rule: _Rule, dir_fd: int | None, name: str) -> int:
    """An open descriptor of a regular file that passes *rule* and the size cap; closed again on every refusal."""
    try:  # O_NONBLOCK: a FIFO in place of the file must not wait for a writer
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP: a symlink in place of the file; EACCES: not readable
        raise RegistryPermissionError(f"{name}: cannot open safely ({type(exc).__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):  # before fdopen, which cannot wrap a directory
            raise RegistryCorruptError(f"{name}: not a regular file")
        if st.st_uid != rule.uid or stat.S_IMODE(st.st_mode) & rule.forbidden:
            raise RegistryPermissionError(f"{name}: must be owned by {rule.who} with the published mode, no wider")
        if st.st_size > MAX_FILE_BYTES:
            raise RegistryCorruptError(f"{name}: larger than {MAX_FILE_BYTES} bytes")
    except OSError as exc:  # fstat failed (EIO and the like): the same typed refusal as a failed open
        os.close(fd)
        raise RegistryPermissionError(f"{name}: cannot inspect it ({type(exc).__name__})") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_checked(path: os.PathLike[str] | str, rule: _Rule, dir_fd: int | None = None) -> bytes:
    """The bytes of a regular file owned by ``rule.uid`` without a mode bit in ``rule.forbidden``.

    *path* is relative to *dir_fd* when that is given. ``FileNotFoundError`` if missing. Never follows a symlink,
    never blocks on a FIFO, never opens for writing, reads at most ``MAX_FILE_BYTES``. Every other OS error is a
    ``RegistryPermissionError`` naming the file and the error type only; no descriptor outlives the call.
    """
    name = Path(path).name
    fd = _checked_fd(path, rule, dir_fd, name)
    try:
        fh = os.fdopen(fd, "rb")
    except OSError as exc:  # EMFILE and the like: fdopen did not take the descriptor over
        os.close(fd)
        raise RegistryPermissionError(f"{name}: cannot read it ({type(exc).__name__})") from None
    try:
        with fh:
            raw = fh.read(MAX_FILE_BYTES + 1)  # bounded even if the file grew after the size check
    except OSError as exc:  # EIO and the like: the same typed refusal as a failed open
        raise RegistryPermissionError(f"{name}: cannot read it ({type(exc).__name__})") from None
    if len(raw) > MAX_FILE_BYTES:
        raise RegistryCorruptError(f"{name}: larger than {MAX_FILE_BYTES} bytes")
    return raw


@contextlib.contextmanager
def _public_dir(directory: Path, signer_uid: int) -> Iterator[int]:
    """The signer's directory, opened once and checked on the open fd; the caller opens the file relative to it.

    Refused: a symlink, a directory owned by the plugin's own user (never the signer's: the factory refuses that
    pair) and one that group or others can write to. ``FileNotFoundError`` if it is missing.
    """
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP / ENOTDIR: a symlink or something that is not a directory
        raise RegistryPermissionError(f"public dir: cannot open safely ({type(exc).__name__})") from None
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise RegistryPermissionError(f"public dir: cannot inspect it ({type(exc).__name__})") from None
    try:
        if st.st_uid == os.geteuid() != signer_uid or stat.S_IMODE(st.st_mode) & 0o022:
            raise RegistryPermissionError("public dir: owned by the plugin user, or writable by group or others")
        yield fd
    finally:
        os.close(fd)


def read_signer_file(directory: Path, signer_uid: int, name: str) -> bytes:
    """*name* in the signer's *directory*, read with the checks above (directory, then file owned by *signer_uid*,
    mode ``0640`` or stricter, regular, no symlink, size cap). ``FileNotFoundError`` if either is missing."""
    rule = _Rule(signer_uid, _SIGNER_FORBIDDEN, "the signer user")
    with _public_dir(directory, signer_uid) as dir_fd:
        return _read_checked(name, rule, dir_fd)


class SignerFileRegistry(Registry):
    """``Registry`` over the signer's ``devices.json`` in *public_dir*, owned by *signer_uid*; read-only, no cache."""

    def __init__(self, public_dir: os.PathLike[str] | str, signer_uid: int, overlay: DenyOverlay | None = None) -> None:
        super().__init__(public_dir)
        self._signer_uid, self._overlay = signer_uid, overlay

    def _read(self) -> list[dict[str, Any]]:
        try:
            raw = read_signer_file(self._dir, self._signer_uid, FILE_NAME)
        except FileNotFoundError:
            raise RegistryMissingError(f"{FILE_NAME}: missing - the signer has not published it") from None
        entries = parse(raw)
        if self._overlay is not None:
            self._overlay.apply(entries)
        return entries

    def _refuse(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        """Every writer of ``Registry``, before any argument is looked at (so no name validation comes first)."""
        raise ReadOnlyRegistryError(f"{FILE_NAME}: the signer's registry is read-only for the plugin")

    add = revoke = _write = _transaction = _open_lock = _sweep_temps = _refuse


def signer_file_registry(cfg: GlassesConfig, overlay: DenyOverlay | None = None) -> SignerFileRegistry:
    """The reader for ``signer.public_dir`` and ``signer.user`` (typed errors from the signer client when unusable)."""
    return SignerFileRegistry(public_file(cfg, PUBLIC_REGISTRY).parent, signer_user_uid(cfg), overlay)


def _no_repeated_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("repeated key")
    return dict(pairs)


def _load_map(path: Path, key: str) -> dict[str, str]:
    """``{deviceId: time}`` from a plugin-owned record ``{"version": 1, key: {...}}``; ``{}`` if there is no file."""
    try:
        raw = _read_checked(path, _Rule(os.geteuid(), _OWNER_FORBIDDEN, "the plugin user"))
    except FileNotFoundError:
        return {}
    try:
        doc = json.loads(raw, object_pairs_hook=_no_repeated_key)
    except (ValueError, RecursionError):
        raise RegistryCorruptError(f"{path.name}: not valid JSON") from None
    records = doc.get(key) if isinstance(doc, dict) and set(doc) == {"version", key} else None
    if not (isinstance(records, dict) and type(doc["version"]) is int and doc["version"] == FORMAT_VERSION):
        raise RegistryCorruptError(f"{path.name}: malformed record")
    if not all(ID_RE.fullmatch(k) and isinstance(v, str) and _TIME_RE.fullmatch(v) for k, v in records.items()):
        raise RegistryCorruptError(f"{path.name}: malformed record")
    return dict(records)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_file(directory: Path, name: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, directory / name)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)  # only left over when the write or the rename failed
    _fsync_dir(directory)


def _write_record(directory: Path, name: str, key: str, records: dict[str, str]) -> None:
    """Atomically replace ``directory/name`` (a private dir, made 0700 if absent) with the record; ``RegistryError``."""
    text = json.dumps({"version": FORMAT_VERSION, key: records}, indent=2, sort_keys=True) + "\n"
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory, 0o700)
        st = os.stat(directory)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise RegistryPermissionError("state dir: must be a directory owned by the plugin user, mode 700")
        _replace_file(directory, name, text)
    except OSError as exc:
        raise RegistryError(f"{name}: cannot write it ({type(exc).__name__})") from None


class DenyOverlay:
    """``revoked-local.json``: the plugin's own revocations, narrow-only (see the module docstring)."""

    def __init__(self, state_dir: os.PathLike[str] | str, clock: Callable[[], datetime] = _utcnow) -> None:
        self._dir = Path(state_dir)
        self._clock = clock
        self._lock = threading.Lock()
        self._denied = _load_map(self._dir / OVERLAY_NAME, "revoked")
        self._unsaved = False  # a write failed: memory is ahead of the file

    def is_denied(self, device_id: str) -> bool:
        return device_id in self._denied

    def denied_at(self, device_id: str) -> str | None:
        return self._denied.get(device_id)

    def deny(self, device_id: str) -> None:
        """Record the revocation of *device_id* (kept for good; the first time stays). It writes and fsyncs: never from
        the event loop; the memory update comes first, so the device is denied even when the write raises, and the
        next ``deny`` (of any device) writes the whole map again until one write succeeds."""
        if not (isinstance(device_id, str) and ID_RE.fullmatch(device_id)):
            raise InvalidDeviceError("deviceId: must be 16 lower-case hex characters")
        with self._lock:
            if device_id in self._denied and not self._unsaved:
                return
            if device_id not in self._denied:
                self._denied = {**self._denied, device_id: _stamp(self._clock())}  # a new dict: readers take no lock
            self._unsaved = True
            _write_record(self._dir, OVERLAY_NAME, "revoked", self._denied)
            self._unsaved = False

    def apply(self, entries: list[dict[str, Any]]) -> None:
        """Mark the registry *entries* of denied devices revoked, as the signer's own revoke would."""
        for entry in entries:
            when = self._denied.get(entry["deviceId"])
            if when is not None and entry["revokedAt"] is None:
                entry["revokedAt"], entry["tokenSha256"] = when, None


class LastSeen:
    """``last-seen.json``: when each device last authenticated (see the module docstring)."""

    def __init__(
        self,
        state_dir: os.PathLike[str] | str,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dir = Path(state_dir)
        self._clock, self._monotonic = clock, monotonic
        self._lock = threading.Lock()
        self._seen = self._load()
        self._touched: dict[str, float] = {}  # monotonic time of each device's last write: the wall clock can step
        self._queued = self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glasses-last-seen")

    def _load(self) -> dict[str, str]:
        try:
            return _load_map(self._dir / LAST_SEEN_NAME, "seen")
        except RegistryError as exc:  # display only: start empty, the next write replaces the file
            _LOG.warning("glasses last-seen: ignoring the old file (%s)", type(exc).__name__)
            return {}

    def touch(self, device: Device) -> None:
        """Note a successful upgrade; returns at once (the write runs on the writer thread)."""
        now, tick = self._clock(), self._monotonic()
        with self._lock:
            last = self._touched.get(device.device_id)
            if self._closed or (last is not None and tick - last < MIN_INTERVAL_S):
                return
            self._touched[device.device_id] = tick
            self._seen[device.device_id] = _stamp(now)
            queued, self._queued = self._queued, True
            if not queued:  # under the lock: close() cannot shut the pool down between the check and the submit
                self._pool.submit(self._flush)

    def _flush(self) -> None:
        with self._lock:
            self._queued = False
            records = dict(self._seen)
        try:
            self._write(records)
        except Exception as exc:  # noqa: BLE001 - display only: log the type, the next touch writes everything again
            # a RegistryError message is fixed text (file name, error type); anything else: the type name only
            _LOG.warning("glasses last-seen: %s", exc if isinstance(exc, RegistryError) else type(exc).__name__)

    def _write(self, records: dict[str, str]) -> None:
        _write_record(self._dir, LAST_SEEN_NAME, "seen", records)

    def close(self) -> None:
        """Stop taking touches; blocks until the write in flight ends; call it off the loop."""
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True)
