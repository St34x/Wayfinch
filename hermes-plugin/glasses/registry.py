# SPDX-License-Identifier: Apache-2.0
"""Device registry: ``devices.json`` in the plugin state dir (docs/architecture.md section 4).

Entry keys: ``deviceId``, ``name``, ``tokenSha256``, ``certFingerprint`` (SHA-256 of the client
cert, lower-case hex), ``lanes``, ``features``, ``createdAt``, ``lastSeen``, ``revokedAt``. The
256-bit token is never stored (its SHA-256 is enough); ``add`` returns it exactly once.

Invariants: ``lookup`` compares hashes and fingerprints with ``hmac.compare_digest`` against every
entry (revoked, unknown, mismatched → no device). ``revoke`` sets ``revokedAt`` and drops the token
hash; the fingerprint stays as a tombstone that ``lookup`` reports (HTTP 403, not 401); a
fingerprint is never enrolled twice, an id never reused. Writes are atomic (temp file in the same
dir, fsync, rename, dir fsync — a failed dir fsync is only logged) under an exclusive ``flock`` on a
checked lock file (plugin and admin CLI may both write); stale temps are swept under the lock.
Fail closed: a corrupt file (bad JSON, too deep or too large, a non-regular file, or an entry breaking the
``add`` rules; the read rules live in ``registry_parse``), group/other access, a foreign owner or a symlink
raise a typed ``RegistryError``. ``add`` refuses (``RegistryFullError``, file untouched) a result over the read
cap, so the cap never locks out ``revoke``. No token or token hash appears in a log line or an exception message.
"""

from __future__ import annotations

import builtins
import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .registry_parse import (
    FILE_NAME,
    FORMAT_VERSION,
    MAX_FILE_BYTES,
    InvalidDeviceError,
    RegistryCorruptError,
    RegistryError,
    check_lanes,
    check_name,
    norm_fp,
    parse,
)

logger = logging.getLogger(__name__)

LOCK_NAME = FILE_NAME + ".lock"
FILE_MODE = 0o600
DIR_MODE = 0o700
TOKEN_BYTES = 32
ID_BYTES = 8
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")


class RegistryPermissionError(RegistryError):
    pass


class UnknownDeviceError(RegistryError):
    pass


class RegistryFullError(RegistryError):
    """``add`` would take ``devices.json`` over the read cap (nothing was written)."""


@dataclass(frozen=True)
class Device:
    """A registry entry as callers see it (no token hash)."""

    device_id: str
    name: str
    cert_fingerprint: str
    lanes: tuple[str, ...]
    features: tuple[str, ...]
    created_at: str
    last_seen: str | None
    revoked_at: str | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _credentials(token: Any, fingerprint: Any) -> tuple[str, str] | None:
    """``(token, normalised fingerprint)`` when both have the right shape, else None."""
    fp = norm_fp(fingerprint)
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or fp is None:
        return None
    return token, fp


def _active(entries: list[dict[str, Any]], credentials: tuple[str, str] | None) -> Device | None:
    if credentials is None:
        return None
    token, fp = credentials
    digest = _token_hash(token)
    found: dict[str, Any] | None = None
    for e in entries:
        token_ok = hmac.compare_digest(e["tokenSha256"] or "", digest)
        fp_ok = hmac.compare_digest(e["certFingerprint"], fp)
        if token_ok and fp_ok and e["revokedAt"] is None:
            found = e
    return _to_device(found) if found is not None else None


def _tombstoned(entries: list[dict[str, Any]], fingerprint: Any) -> bool:
    fp = norm_fp(fingerprint)
    if fp is None:
        return False
    return any([hmac.compare_digest(e["certFingerprint"], fp) for e in entries if e["revokedAt"] is not None])


def _to_device(e: dict[str, Any]) -> Device:
    return Device(
        device_id=e["deviceId"],
        name=e["name"],
        cert_fingerprint=e["certFingerprint"],
        lanes=tuple(e["lanes"]),
        features=tuple(e["features"]),
        created_at=e["createdAt"],
        last_seen=e["lastSeen"],
        revoked_at=e["revokedAt"],
    )


def _check_owner_mode(st: os.stat_result, what: str, want: int) -> None:
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise RegistryPermissionError(
            f"{what}: must be owned by the plugin user with mode {want:o} (no group/other access)"
        )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Registry:
    """``devices.json`` under *state_dir* (created 0700 on the first write; its parent must exist)."""

    def __init__(self, state_dir: os.PathLike[str] | str, clock: Callable[[], datetime] = _utcnow) -> None:
        self._dir = Path(state_dir)
        self._file = self._dir / FILE_NAME
        self._clock = clock

    def _now(self) -> str:
        return self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _check_dir(self) -> bool:
        """False if the state dir does not exist yet; raises if it is not private."""
        try:
            st = os.stat(self._dir)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(st.st_mode):
            raise RegistryPermissionError("state dir: not a directory")
        _check_owner_mode(st, "state dir", DIR_MODE)
        return True

    def _open_data(self) -> int | None:
        """The data file, opened without blocking or following a link and checked; None if it does not exist."""
        try:  # O_NONBLOCK: opening a FIFO must not wait for a writer while the lock is held
            fd = os.open(self._file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:  # ELOOP: a symlink in place of the file
            raise RegistryPermissionError(f"{FILE_NAME}: cannot open safely ({type(exc).__name__})") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):  # a directory, FIFO or device
                raise RegistryCorruptError(f"{FILE_NAME}: not a regular file")
            _check_owner_mode(st, FILE_NAME, FILE_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _read(self) -> list[dict[str, Any]]:
        if not self._check_dir():
            return []
        fd = self._open_data()
        if fd is None:
            return []
        with os.fdopen(fd, "rb") as fh:
            if os.fstat(fh.fileno()).st_size > MAX_FILE_BYTES:
                raise RegistryCorruptError(f"{FILE_NAME}: larger than {MAX_FILE_BYTES} bytes")
            raw = fh.read(MAX_FILE_BYTES + 1)  # bounded even if the file grew after the size check
            if len(raw) > MAX_FILE_BYTES:
                raise RegistryCorruptError(f"{FILE_NAME}: larger than {MAX_FILE_BYTES} bytes")
            return parse(raw)

    def _write(self, entries: list[dict[str, Any]], max_bytes: int | None = None) -> None:
        data = json.dumps({"version": FORMAT_VERSION, "devices": entries}, indent=2, sort_keys=True) + "\n"
        if max_bytes is not None and len(data.encode("utf-8")) > max_bytes:
            raise RegistryFullError(f"registry full: {FILE_NAME} would exceed {max_bytes} bytes")
        fd, tmp = tempfile.mkstemp(prefix=f".{FILE_NAME}.", suffix=".tmp", dir=self._dir)  # mode 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._file)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)  # only left over when the write or the rename failed
        try:
            _fsync_dir(self._dir)
        except OSError as exc:  # the rename is done, so the change is committed: do not lose a new token
            logger.warning("glasses registry: state dir fsync failed (%s)", type(exc).__name__)

    def _open_lock(self) -> int:
        """The lock file, opened and checked like the data file (no symlink, owner only)."""
        try:
            fd = os.open(self._dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
        except OSError as exc:  # ELOOP: a symlink in place of the lock
            raise RegistryPermissionError(f"{LOCK_NAME}: cannot open safely ({type(exc).__name__})") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RegistryPermissionError(f"{LOCK_NAME}: not a regular file")
            _check_owner_mode(st, LOCK_NAME, FILE_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _sweep_temps(self) -> None:
        """Best effort: drop temp files a crashed writer left (temps only exist while the lock is held)."""
        for tmp in self._dir.glob(f".{FILE_NAME}.*.tmp"):
            with contextlib.suppress(OSError):
                tmp.unlink()

    @contextlib.contextmanager
    def _transaction(self, max_bytes: int | None = None) -> Iterator[list[dict[str, Any]]]:
        """Read-modify-write under an exclusive lock; the file is written only if the body succeeds.

        *max_bytes* refuses (``RegistryFullError``, nothing written) a result over that size: only ``add`` sets
        it, because a file over the read cap would lock out every later read, ``revoke`` included.
        """
        try:
            os.mkdir(self._dir, DIR_MODE)
        except FileExistsError:
            pass
        except FileNotFoundError:
            raise RegistryError("state dir: parent directory does not exist") from None
        self._check_dir()
        fd = self._open_lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._sweep_temps()
            entries = self._read()
            yield entries
            self._write(entries, max_bytes)
        finally:
            os.close(fd)  # releases the lock

    def add(self, name: str, fingerprint: str, lanes: builtins.list[str]) -> tuple[Device, str]:
        """Enrol a device: new id and new 256-bit token. The token is returned once and never stored."""
        check_name(name)
        fp = norm_fp(fingerprint)
        if fp is None:
            raise InvalidDeviceError("fingerprint: must be the SHA-256 of the client certificate (64 hex chars)")
        lane_list = check_lanes(lanes)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._transaction(MAX_FILE_BYTES) as entries:
            if any(e["certFingerprint"] == fp for e in entries):
                raise InvalidDeviceError("fingerprint: already enrolled (active or revoked); enrol a new key")
            ids = {e["deviceId"] for e in entries}
            device_id = secrets.token_hex(ID_BYTES)
            while device_id in ids:
                device_id = secrets.token_hex(ID_BYTES)
            entry = {
                "deviceId": device_id,
                "name": name,
                "tokenSha256": _token_hash(token),
                "certFingerprint": fp,
                "lanes": lane_list,
                "features": [],
                "createdAt": self._now(),
                "lastSeen": None,
                "revokedAt": None,
            }
            entries.append(entry)
        logger.info("glasses registry: device added deviceId=%s lanes=%d", device_id, len(lane_list))
        return _to_device(entry), token

    def lookup(self, token: Any, fingerprint: Any) -> tuple[Device | None, bool]:
        """The upgrade's one read: ``(device, False)`` for the active device holding this token and client-cert
        fingerprint (constant-time), else ``(None, tombstoned)``, *tombstoned* True for a revoked fingerprint."""
        entries = self._read()
        device = _active(entries, _credentials(token, fingerprint))
        return (device, False) if device is not None else (None, _tombstoned(entries, fingerprint))

    def revoke(self, device_id: str) -> Device:
        """Registry revocation: ``revokedAt`` set, token hash dropped, fingerprint kept as tombstone. Idempotent."""
        with self._transaction() as entries:
            entry = next((e for e in entries if e["deviceId"] == device_id), None)
            if entry is None:
                raise UnknownDeviceError("revoke: unknown device id")
            if entry["revokedAt"] is None:
                entry["revokedAt"] = self._now()
                entry["tokenSha256"] = None
        logger.info("glasses registry: device revoked deviceId=%s", device_id)
        return _to_device(entry)

    def list(self) -> builtins.list[Device]:
        """Every entry, revoked ones included (``revoked_at`` set)."""
        return [_to_device(e) for e in self._read()]
