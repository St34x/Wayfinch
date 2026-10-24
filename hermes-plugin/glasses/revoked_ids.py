# SPDX-License-Identifier: Apache-2.0
"""The frame loop's revocation check (T1.248; interim, until the live cut of T1.63a replaces it).

``RevokedIds.is_revoked`` is the ``is_revoked`` the adapter injects (``server_auth.revoked_now`` asks it at the
upgrade and before every frame is dispatched). It answers ``True`` for a device the deny overlay denies, one the outbox
has purged in this process, or one the signer's ``devices.json`` lists as revoked. The last set is taken from the
registry reader (``registry.list``, the T1.179 reader: never a second parser) and answers only for the version of the
file it was read from, keyed by ``(st_mtime_ns, st_ctime_ns, st_size, st_ino)``: the signer replaces the file by an
atomic rename (a new inode even when the mtime does not move), and ``chmod`` / ``chown`` move the ctime. The key is
taken before the read, so a version renamed in during a read is read again.

Who reads: a ``stat`` per check (cheap, on the loop) and a stat poll every ``POLL_S`` (0.5 s) from ``bind()`` to
``close()``; either one starts the re-read of a changed file at once, so no frame or upgrade waits for the poll and an
idle open session of a signer-revoked device is cut within about a second. The read runs on this module's own single
worker thread (never the loop, the outbox worker or the loop's default executor, which the gateway shares), at most
one at a time, never one per frame, bounded by ``READ_TIMEOUT_S`` (2 s); a timed-out read that still hangs in the
worker makes every later attempt fail at once instead of queueing behind it.

While a re-read is in flight, a device not already known as revoked gets ``ReadPending`` carrying the read: the
caller waits for it (the frame or upgrade is held, not dropped) and asks again; when the file changed once more during
that read, the second ask meets the next read and the frame is dropped (an upgrade ``503``, a DEBUG line). A read that
fails or times out, or one of a file whose stat failed, keeps the last set but is never taken as the file's version:
until a read of the current version succeeds, such a device
gets ``RegistryUnread`` (fail closed: a frame is dropped, an upgrade answered ``503``), and the same version is read
again at most once per ``RETRY_S`` (1 s), a new version at once. One WARNING per failure episode, by type.

When a read finds a device newly revoked, the bound cut purges its outbox rows and closes its connection with ``4403``
(one INFO line with the device id when a session was closed); a cut that raises is logged by type and the other
devices are still cut. A poll step that raises is logged (one WARNING per episode, by type) and the poll goes on: only
``close()`` ends it. Not covered here: an idle session of a device revoked
only in the deny overlay (the live cut, T1.63a, planned).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any

from .outbound import DeviceView
from .server_auth import ReadPending as ReadPending

_LOG = logging.getLogger(__name__)
CLOSE_REVOKED = 4403
POLL_S = 0.5  # the stat poll's interval (bind() .. close())
READ_TIMEOUT_S = 2.0  # one registry read; longer is a failed read (fail closed)
RETRY_S = 1.0  # a failed read of an unchanged file is tried again no sooner than this
Key = tuple[int, int, int, int] | None  # (st_mtime_ns, st_ctime_ns, st_size, st_ino); None: the file cannot be stat'ed
Close = Callable[[str, int], Coroutine[Any, Any, bool]]  # ``ConnectionTable.close_device``
_UNREAD = (-1, -1, -1, -1)  # before the first read: every stat differs from it


class RegistryUnread(Exception):
    """The registry's current version could not be read: the check refuses until a read of it succeeds."""


def _key(path: Path) -> Key:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino


def _revoked(devices: Iterable[DeviceView]) -> set[str]:
    return {d.device_id for d in devices if d.revoked_at is not None}


class RevokedIds:
    """The stat-keyed revoked-id set over *path* read by *read*, plus the in-memory checks *also* (see the module)."""

    def __init__(self, path: Path, read: Callable[[], Iterable[DeviceView]], *also: Callable[[str], bool]) -> None:
        self._path, self._read, self._also = path, read, also
        self._ids: frozenset[str] = frozenset()
        self._seen: Key = _UNREAD  # the version ``_ids`` was read from
        self._failed: tuple[Key, float] | None = None  # the version whose last read failed, and when
        self._pending: asyncio.Task[None] | None = None
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="glasses-revoked-ids")
        self._busy: concurrent.futures.Future[set[str]] | None = None  # the worker's read (a timed-out one may run on)
        self._cuts: set[asyncio.Task[None]] = set()  # held until they end (the loop keeps weak references only)
        self._poll: asyncio.Task[None] | None = None
        self._close: Close | None = None
        self._purge: Callable[[str], object] | None = None

    def bind(self, close: Close, purge: Callable[[str], object]) -> None:
        """The cut for a device a read finds newly revoked (*purge*, sync, then *close* ``4403``); starts the poll."""
        self._close, self._purge = close, purge
        if self._poll is None:
            self._poll = asyncio.get_running_loop().create_task(self._polling(), name="glasses-revoked-ids-poll")

    def close(self) -> None:
        """Stop the poll, a read in flight and the cuts; shut the worker down (not joined). Never raises."""
        for task in (self._poll, self._pending, *self._cuts):
            if task is not None:
                task.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def is_revoked(self, device_id: str) -> bool:
        """``True`` revoked, ``False`` active; ``ReadPending`` in a re-read, ``RegistryUnread`` after a failed one."""
        if device_id in self._ids or any(check(device_id) is True for check in self._also):
            return True
        key = _key(self._path)
        if key == self._seen:
            return False
        self._refresh(key)
        if self._pending is not None:
            raise ReadPending(self._pending)
        raise RegistryUnread("glasses: the device registry changed and could not be read; refusing")

    async def load(self) -> None:
        """Read the file now (``connect()``, before the server serves): the first check then needs no re-read."""
        await self._reload(_key(self._path))

    def _refresh(self, key: Key) -> None:
        """Start the read of version *key*, unless a read is in flight or one of *key* failed under ``RETRY_S`` ago."""
        failed = self._failed
        if self._pending is not None or (failed is not None and failed[0] == key and _since(failed[1]) < RETRY_S):
            return
        self._pending = asyncio.get_running_loop().create_task(self._reload(key), name="glasses-revoked-ids")

    async def _polling(self) -> None:
        failing = False  # one WARNING per episode of failing poll steps; the poll never ends but by close()
        while True:
            await asyncio.sleep(POLL_S)
            try:
                await self._poll_step()
            except Exception as exc:  # noqa: BLE001 - an idle revoked session must still be cut later; type only
                if not failing:
                    _LOG.warning("glasses revoked ids: poll step failed (%s); polling on", type(exc).__name__)
                failing = True
            else:
                failing = False

    async def _poll_step(self) -> None:
        key = _key(self._path)
        if key != self._seen:
            self._refresh(key)
        if self._pending is not None:
            await asyncio.wait((self._pending,))

    async def _read_set(self) -> set[str]:
        if self._busy is not None and not self._busy.done():
            raise TimeoutError  # the last read still hangs in the worker: never queue another one behind it
        self._busy = busy = self._pool.submit(lambda: _revoked(self._read()))
        return await asyncio.wait_for(asyncio.wrap_future(busy), READ_TIMEOUT_S)

    async def _reload(self, key: Key) -> None:
        try:
            found = await self._read_set()
            if key is None:  # the file could not be stat'ed: a read without a version is never taken as read
                raise RegistryUnread("glasses: the device registry could not be stat'ed")
        except asyncio.CancelledError:
            self._pending = None  # nothing recorded: the next check reads again
            raise
        except Exception as exc:  # noqa: BLE001 - keep the last set, never take this version as read; type only
            if self._failed is None:
                _LOG.warning("glasses revoked ids: registry read failed (%s); refusing", type(exc).__name__)
            self._failed, self._pending = (key, time.monotonic()), None
            return
        self._seen, self._failed, self._pending, fresh = key, None, None, found - self._ids
        self._ids = self._ids | found  # a revocation is never undone (a re-enrolled phone gets a new id)
        for device_id in sorted(fresh):
            try:
                self._cut(device_id)
            except Exception as exc:  # noqa: BLE001 - the next device is still cut; this one stays revoked
                _LOG.warning("glasses revoked ids: cutting a revoked device failed (%s)", type(exc).__name__)

    def _cut(self, device_id: str) -> None:
        if self._purge is None:
            return  # not serving yet (the load at start): no connection to close
        self._purge(device_id)
        task = asyncio.get_running_loop().create_task(self._closed(device_id))
        self._cuts.add(task)
        task.add_done_callback(self._cuts.discard)

    async def _closed(self, device_id: str) -> None:
        try:
            if self._close is not None and await self._close(device_id, CLOSE_REVOKED) is True:
                _LOG.info("glasses revoked ids: open session of a revoked device closed 4403 (deviceId=%s)", device_id)
        except Exception as exc:  # noqa: BLE001 - the device stays revoked for every later check; type only
            _LOG.warning("glasses revoked ids: closing a revoked device failed (%s)", type(exc).__name__)


def _since(at: float) -> float:
    return time.monotonic() - at
