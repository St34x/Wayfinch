# SPDX-License-Identifier: Apache-2.0
"""The outbox (T1.09b): numbered S→D frames per device in ``outbox.sqlite`` until acked, evicted or purged.

``Store`` is the file (tables ``rows``, ``counters``, ``purged``; created 0600 in the plugin state dir, deleted text
overwritten by ``secure_delete``); every call runs on the ``Outbox``'s own single worker thread, never on the event
loop or its default executor. ``Outbox`` is the loop side:

* ``send(list_devices, lane, build)``: one row per recipient (``outbound.recipients``) with the device's next ``seq``
  (never above 2**53 - 1: at the limit the device gets no row); every revoked device is purged instead, all in one
  transaction (a failure stores and purges nothing, so the gateway's retry gives no duplicate and no gap).
  A device welcomed on a live connection gets the frame written at once; ``True`` means stored for at least one.
* TTL and cap: unpinned rows of every device older than ``TTL_S`` (24 h) go on every store, ``hello`` and ``open``;
  over ``CAP`` (500) rows of one device the oldest unpinned rows go (``push`` first) and its one queued ``error``
  ``outbox_overflow`` row (kind ``overflow``) is replaced by a new one with the next ``seq``.
  Pinned rows (approval and clarify cards never expire; no producer yet) are never evicted by TTL or cap.
* ``hello(device_id, lastAckedSeq, link)``: a device the registry (``devices``, read once per ``hello`` on the worker)
  lists as revoked is purged in the same transaction and gets nothing; otherwise the counter is raised to
  ``lastAckedSeq``, rows up to it and expired rows go, and the device's queued frames come back in ``seq`` order with
  ``resumeFrom`` (the first of them, else the next ``seq``). The server writes ``welcome`` and these through the
  connection's one send queue; from then on ``send`` writes only frames numbered after them, so a live frame never
  overtakes or repeats a replayed one. A write the link refuses drops the link (the row stays for the next ``hello``)
  and calls ``link.refused()``; ``closed(device_id, link)`` (the server's ``on_closed``) drops it when its session ends,
  unless a newer connection of the device was welcomed meanwhile.
* ``ack_soon(device_id, upToSeq)``: rows up to ``upToSeq`` go (queued on the worker; the frame loop does not wait).
* ``purge_soon(device_id)`` / ``purge`` (revocation): every row and the counter of the device go, the device is recorded
  in ``purged`` and nothing is ever stored or replayed for it again; its live link and its inbound dedupe ids
  (``forget``) are dropped at once. A ``hello`` read already in flight hands out no replay when the device is purged
  by id meanwhile, and when a tombstone refusal lands meanwhile (``on_revoked(None)`` counts it; the ``hello`` then
  waits for the sweep queued behind its read and asks again, before the link is registered; a sweep that could not
  read the registry refuses that ``hello`` with ``TryAgainLater``: the server closes it ``1013`` without ``welcome``,
  its rows stay for the next ``hello``). A ``hello`` the registry refuses drops the device the same way (its link and
  dedupe ids). It runs where a revoked device is seen: ``send`` (every revoked registry device), ``on_revoked`` (the
  server refused a revoked device: its id, or a coalesced sweep for a fingerprint tombstone) and ``sweep(list_devices)``
  in ``connect()``, after ``open`` has checked the file.

No message text reaches a log line; device ids and counts do.

Limits, by design: a device that sends ``hello.lastAckedSeq`` = 2**53 - 1 gets no outbox row again (it muted itself; one
WARNING per device and process; re-enrolment gives a new id). A device once purged never gets rows again
(``purged`` is never emptied), also when the plugin user removes its id from ``revoked-local.json`` and it
authenticates after a restart: fails closed (a revoked id is never reused). The state directory must be owned by the
plugin user, mode 0700 (only 0700 is supported: last-seen, the deny overlay and ``crl --check`` refuse any other); the
outbox alone tolerates a readable 0755 one (file 0600) and refuses a group- or world-writable one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path
from typing import Protocol, TypeVar

from .outbound import DeviceView, recipients
from .server_auth import TryAgainLater

_LOG = logging.getLogger(__name__)
T = TypeVar("T")

FILE_NAME = "outbox.sqlite"
MAX_SEQ = 2**53 - 1  # the schema maximum of a seq
TTL_S = 24 * 3600.0
CAP = 500  # rows per device
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS rows(device_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,"
    " pinned INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, frame TEXT NOT NULL, PRIMARY KEY(device_id, seq))",
    "CREATE TABLE IF NOT EXISTS counters(device_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS purged(device_id TEXT PRIMARY KEY, purged_at REAL NOT NULL)",
)
Stored = list[tuple[int, str]]  # (seq, encoded frame)
Devices = Callable[[], Iterable[DeviceView]]
Delivered = tuple[dict[str, Stored], list[str]]  # (stored rows per recipient, purged revoked devices)


class OutboxError(Exception):
    """The outbox file is unusable (wrong type, owner or mode)."""


class Link(Protocol):
    """The live connection of a welcomed device (``connections.Connection``)."""

    def send(self, text: str) -> bool: ...

    def refused(self) -> None: ...


def _open_private(path: Path) -> sqlite3.Connection:
    """The database at *path*, created 0600 (its directory 0700) when absent; any other file is refused, and so is an
    existing directory of another owner or one that group or others can write to (a swap of the file needs write
    access to it). Only this module tolerates a readable 0755 one (the file is 0600); the plugin supports 0700 only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = path.parent.stat()
    if folder.st_uid != os.geteuid() or stat.S_IMODE(folder.st_mode) & 0o022:
        raise OutboxError(f"{FILE_NAME}: its directory must belong to the plugin user, not be group- or world-writable")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise OutboxError(f"{FILE_NAME}: must be a regular file of the plugin user, mode 600")
    db = sqlite3.connect(path)
    db.execute("PRAGMA secure_delete=ON")  # a deleted row's text is overwritten, not left in a free page
    for statement in _SCHEMA:
        db.execute(statement)
    return db


class Store:
    """The SQLite file; every method runs on the outbox worker thread (one writer). *overflow* encodes the
    ``outbox_overflow`` frame for a ``seq``; *clock* is wall-clock seconds."""

    def __init__(self, path: Path, clock: Callable[[], float], overflow: Callable[[int], str]) -> None:
        self._path, self._clock, self._overflow = path, clock, overflow
        self._conn: sqlite3.Connection | None = None
        self._limit_logged: set[str] = set()  # devices whose "seq limit reached" WARNING was logged (once per process)
        self._capped: list[tuple[str, int]] = []  # (device, frames dropped over the cap) to log after the commit

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _open_private(self._path)
        return self._conn

    def _last(self, db: sqlite3.Connection, device_id: str) -> int:
        row = db.execute("SELECT last_seq FROM counters WHERE device_id=?", (device_id,)).fetchone()
        return int(row[0]) if row else 0

    def _purged(self, db: sqlite3.Connection, device_id: str) -> bool:
        return db.execute("SELECT 1 FROM purged WHERE device_id=?", (device_id,)).fetchone() is not None

    def _expire(self, db: sqlite3.Connection) -> None:  # every device's, not only the one written or greeted
        db.execute("DELETE FROM rows WHERE pinned=0 AND created_at<?", (self._clock() - TTL_S,))

    def _put(self, db: sqlite3.Connection, device_id: str, build: Callable[[int], str], kind: str) -> Stored:
        seq = self._last(db, device_id) + 1
        if seq > MAX_SEQ:
            if device_id not in self._limit_logged:  # one WARNING per device: a muted device sends this on every reply
                self._limit_logged.add(device_id)
                _LOG.warning("glasses outbox: seq limit reached, frames not stored (deviceId=%s)", device_id)
            return []
        frame = build(seq)
        db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?)", (device_id, seq))
        db.execute("INSERT INTO rows VALUES (?, ?, ?, 0, ?, ?)", (device_id, seq, kind, self._clock(), frame))
        return [(seq, frame)]

    def _over_cap(self, db: sqlite3.Connection, device_id: str) -> Stored:
        (count,) = db.execute("SELECT count(*) FROM rows WHERE device_id=?", (device_id,)).fetchone()
        if count <= CAP:
            return []
        # one queued overflow row per device: the old one goes and the new one takes the last seq
        count -= db.execute("DELETE FROM rows WHERE device_id=? AND kind='overflow'", (device_id,)).rowcount
        victims = db.execute(
            "SELECT seq FROM rows WHERE device_id=? AND pinned=0 ORDER BY kind!='push', seq LIMIT ?",
            (device_id, max(count - CAP + 1, 0)),  # room for the overflow row; never a negative (unbounded) LIMIT
        ).fetchall()
        db.executemany("DELETE FROM rows WHERE device_id=? AND seq=?", [(device_id, seq) for (seq,) in victims])
        self._capped.append((device_id, len(victims)))  # logged after the commit (_log_capped)
        return self._put(db, device_id, self._overflow, "overflow")

    def _log_capped(self) -> None:
        capped, self._capped = self._capped, []
        for device_id, dropped in capped:
            _LOG.info("glasses outbox: %d frames over the cap dropped (deviceId=%s)", dropped, device_id)

    def open(self) -> None:
        """Open (or create) and check the file now, and expire old rows: an unusable file fails ``connect()``."""
        with self._db() as db:  # sqlite3's context manager gives the connection itself
            self._expire(db)

    def _add(self, db: sqlite3.Connection, device_id: str, build: Callable[[int], str]) -> Stored:
        if self._purged(db, device_id):
            return []
        self._expire(db)
        stored = self._put(db, device_id, build, "answer")
        return stored + self._over_cap(db, device_id) if stored else []

    def add(self, device_id: str, build: Callable[[int], str]) -> Stored:
        """Store one answer (and an overflow row when over the cap); ``[]`` for a purged device. The single-device
        store API: ``deliver`` is the production path, this one is kept for the store-level tests."""
        db = self._db()
        self._capped.clear()
        with db:
            stored = self._add(db, device_id, build)
        self._log_capped()  # after the commit: a rolled-back drop is not logged
        return stored

    def deliver(self, devices: Devices, lane: str, build: Callable[[str, int], str]) -> Delivered:
        """Purge every revoked device, store one answer for each active one holding *lane*: ``(stored, purged)``.
        One transaction: a failure for any device purges and stores nothing, so a retry gives no duplicate or gap.
        A device purged before is not purged again."""
        to, revoked = recipients(devices(), lane)
        db = self._db()
        self._capped.clear()
        with db:
            for device_id in revoked:
                if not self._purged(db, device_id):
                    self._purge(db, device_id)
            stored = {device_id: self._add(db, device_id, partial(build, device_id)) for device_id in to}
        self._log_capped()
        return stored, revoked

    def hello(self, device_id: str, acked: int, devices: Devices | None = None) -> tuple[int, Stored, int, bool]:
        """``(resumeFrom, queued rows in seq order, last seq, refused)`` after the ``hello.lastAckedSeq`` purge and the
        TTL. A device *devices* (the registry, read once here) lists as revoked is purged first, in the same
        transaction; it and a device purged before are refused (``True``: nothing replayed)."""
        revoked = devices is not None and any(d.device_id == device_id and d.revoked_at is not None for d in devices())
        db = self._db()
        with db:
            if revoked:
                self._purge(db, device_id)
            if revoked or self._purged(db, device_id):
                return min(acked + 1, MAX_SEQ), [], MAX_SEQ, True  # nothing is replayed or written live, ever
            last = max(self._last(db, device_id), acked)
            db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?)", (device_id, last))
            db.execute("DELETE FROM rows WHERE device_id=? AND seq<=?", (device_id, acked))
            self._expire(db)
            rows = db.execute("SELECT seq, frame FROM rows WHERE device_id=? ORDER BY seq", (device_id,)).fetchall()
        resume = int(rows[0][0]) if rows else min(last + 1, MAX_SEQ)
        return resume, [(int(seq), str(frame)) for seq, frame in rows], last, False

    def ack(self, device_id: str, up_to: int) -> None:
        with self._db() as db:  # no row is ever above the device's last seq: up_to needs no clamp to it
            db.execute("DELETE FROM rows WHERE device_id=? AND seq<=?", (device_id, up_to))

    def _purge(self, db: sqlite3.Connection, device_id: str) -> None:
        db.execute("DELETE FROM rows WHERE device_id=?", (device_id,))
        db.execute("DELETE FROM counters WHERE device_id=?", (device_id,))
        db.execute("INSERT OR IGNORE INTO purged VALUES (?, ?)", (device_id, self._clock()))

    def purge(self, device_id: str) -> None:
        with self._db() as db:
            self._purge(db, device_id)

    def sweep(self, devices: Devices) -> list[str]:
        revoked = [d.device_id for d in devices() if d.revoked_at is not None]
        with self._db() as db:
            for device_id in revoked:
                if not self._purged(db, device_id):  # a purged device holds nothing: add and hello refuse it
                    self._purge(db, device_id)
        return revoked

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _failed(future: concurrent.futures.Future[None]) -> None:
    if not future.cancelled() and (exc := future.exception()) is not None:
        _LOG.warning("glasses outbox: queued ack or purge failed (%s)", type(exc).__name__)


class Outbox:
    """The loop side of ``Store`` (module docstring); *forget* drops a device's inbound dedupe ids (T1.08)."""

    def __init__(self, store: Store, forget: Callable[[str], None], devices: Devices) -> None:
        self._store, self._forget, self._devices = store, forget, devices  # devices: the registry hello checks
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="glasses-outbox")
        self._links: dict[str, tuple[Link, int]] = {}  # welcomed device -> (its connection, last seq written)
        self._revoked: set[str] = set()  # devices purged in this process (bounded by the registry's revoked entries)
        self._sweeper: asyncio.Task[bool] | None = None  # the running tombstone sweep (on_revoked): True when it read
        self._sweep_again = False  # a refusal since the running sweep read the registry: one more sweep follows
        self._refusals = 0  # tombstone refusals so far: a hello whose read saw one land waits for its sweep
        self._ack_lock = threading.Lock()  # guards _acks (loop thread and worker)
        self._acks: dict[str, tuple[int, concurrent.futures.Future[None]]] = {}  # the one pending ack per device

    def is_revoked(self, device_id: str) -> bool:
        """Purged in this process (the frame loop's check asks it too, ``revoked_ids``)."""
        return device_id in self._revoked

    async def _run(self, call: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, call)

    async def send(self, devices: Devices, lane: str, build: Callable[[str, int], str]) -> bool:
        stored, purged = await self._run(partial(self._store.deliver, devices, lane, build))
        for device_id in purged:
            self._drop(device_id)
        for device_id, frames in stored.items():
            for seq, frame in frames:
                self._write(device_id, seq, frame)
        return any(stored.values())

    async def hello(self, device_id: str, acked: int, link: Link) -> tuple[int, list[str]]:
        refusals = self._refusals
        resume, rows, last, refused = await self._run(partial(self._store.hello, device_id, acked, self._devices))
        if refused:  # revoked in the registry (or purged before): its link and dedupe ids go too
            self._drop(device_id)
        swept = True
        if self._refusals != refusals and self._sweeper is not None:  # a tombstone refusal landed during the read
            swept = await asyncio.shield(self._sweeper)  # its sweep (queued behind the read) decides; never cancelled
        if not swept:  # the sweep could not read the registry: no welcome at all, the server closes 1013 (rows kept)
            raise TryAgainLater("glasses outbox: the hello's revocation sweep could not read the registry")
        if device_id in self._revoked:  # purged meanwhile: no replay and no live write
            return min(acked + 1, MAX_SEQ), []
        self._links[device_id] = (link, last)  # no await from here until the server has queued welcome + rows
        return resume, [frame for _, frame in rows]

    def ack_soon(self, device_id: str, up_to: int) -> concurrent.futures.Future[None]:
        """Queue the ack behind every earlier call; the frame loop does not wait for it (a stalled file never holds
        the device's frames). At most one ack per device is pending: a later one joins the pending one in its queue
        position, raises its ``upToSeq`` and shares its future (one worker job and one commit)."""
        with self._ack_lock:
            if device_id in self._acks:
                pending = self._acks[device_id]
                self._acks[device_id] = (max(pending[0], up_to), pending[1])
                return pending[1]
            future = self._soon(partial(self._ack, device_id))
            self._acks[device_id] = (up_to, future)
            return future

    def _ack(self, device_id: str) -> None:  # on the worker: takes the highest pending value
        with self._ack_lock:
            up_to, _ = self._acks.pop(device_id)
        self._store.ack(device_id, up_to)

    def purge_soon(self, device_id: str) -> concurrent.futures.Future[None]:
        """Queue the purge behind every earlier store call (nothing stored before it survives); the link and the
        dedupe ids go now. Safe from the loop's synchronous code (the revocation check)."""
        self._drop(device_id)
        return self._soon(partial(self._store.purge, device_id))

    def _soon(self, call: Callable[[], None]) -> concurrent.futures.Future[None]:
        future = self._pool.submit(call)
        future.add_done_callback(_failed)
        return future

    async def purge(self, device_id: str) -> None:
        await asyncio.wrap_future(self.purge_soon(device_id))

    async def open(self) -> None:
        """Open and check the file on the worker (``connect()``): an unusable one raises ``OutboxError``."""
        await self._run(self._store.open)

    def on_revoked(self, devices: Devices) -> Callable[[str | None], None]:
        """The server's ``on_revoked`` hook (on the loop, ``server_auth.note_revoked``): a refused revoked device is
        purged; a fingerprint tombstone (no device id) sweeps every revoked device of *devices*, coalesced: one sweep
        runs, and refusals meanwhile add at most one more."""

        def revoked(device_id: str | None) -> None:
            if device_id is not None:
                self.purge_soon(device_id)
                return
            self._refusals += 1
            self._sweep_again = True
            if self._sweeper is None or self._sweeper.done():
                task = asyncio.get_running_loop().create_task(self._sweeps(devices), name="glasses-outbox-sweep")
                self._sweeper = task  # held until it ends (the loop keeps only a weak reference)

        return revoked

    async def _sweeps(self, devices: Devices) -> bool:
        swept = True
        while self._sweep_again:  # no await between this check and the task's end: a later refusal starts a new one
            self._sweep_again = False
            swept = await self.sweep(devices) and swept
        return swept

    async def sweep(self, devices: Devices) -> bool:
        """Purge every revoked registry device; ``False`` when the registry cannot be read (logged by type; its revoked
        devices stay undeliverable: authentication refuses them, ``send`` purges them, a waiting ``hello`` is refused
        with ``TryAgainLater``)."""
        try:
            purged = await self._run(partial(self._store.sweep, devices))
        except Exception as exc:  # noqa: BLE001 - the start goes on; type only
            _LOG.warning("glasses outbox: revoked-device sweep failed (%s)", type(exc).__name__)
            return False
        for device_id in purged:
            self._drop(device_id)
        return True

    def _drop(self, device_id: str) -> None:
        self._revoked.add(device_id)
        self._links.pop(device_id, None)
        self._forget(device_id)

    def closed(self, device_id: str, link: Link) -> None:  # on_closed: an entry of a newer connection stays
        if (entry := self._links.get(device_id)) is not None and entry[0] is link:
            del self._links[device_id]

    def _write(self, device_id: str, seq: int, frame: str) -> None:
        entry = self._links.get(device_id)
        if entry is None or seq <= entry[1]:
            return  # not welcomed on a live connection, or the replay already carried it
        link = entry[0]
        if link.send(frame):
            self._links[device_id] = (link, seq)
            return
        del self._links[device_id]  # the row stays and is replayed after the next hello
        link.refused()

    async def close(self) -> None:
        await self._run(self._store.close)
        self._pool.shutdown(wait=False)
