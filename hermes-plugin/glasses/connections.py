# SPDX-License-Identifier: Apache-2.0
"""Live ``/ws`` connections (T1.07c): one per device, each with ONE send queue and ONE writer task.

The writer task writes every frame the server sends (the frame loop writes only the protocol pongs, which are
answers to a ping, never queued), so closing a connection can discard what is still queued and the writer writes
nothing after the close frame. ``Connection.reads_paused`` holds the socket unread for the whole handling of one
frame (the frame loop's yield, its pong writes and ``on_frame``); ``yield_turn`` is its one turn of the event loop
between two frames. ``Connection.pong`` bounds the pong write (``WRITE_TIMEOUT_S``: over it the transport is
aborted). ``Connection.close(code)`` sends the close frame and aborts the transport ``ABORT_AFTER_S`` after it
started when the peer has not completed the close handshake. ``ConnectionTable`` holds the
connections: ``admit`` replaces the device's old connection (closed with ``4409``, the new one is registered first, so
a frame sent from then on goes only to the new one), ``close_device`` is what the revocation push (T1.63a) calls, and
``close_all`` ends every connection with ``1001`` on shutdown. ``Connection.refused`` ends it with ``1008`` after a
refused frame (T1.09b). ``UpgradeCap`` bounds the ``/ws`` upgrades in flight per
key, the verified client-certificate fingerprint (T1.184c); a certificate over the cap is refused before any gate,
registry or thread hop is touched, and other certificates are not affected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from aiohttp import web

_LOG = logging.getLogger(__name__)

CLOSE_REPLACED = 4409
CLOSE_GOING_AWAY = 1001
CLOSE_POLICY = 1008  # a frame the connection refused (queue full or halted): the outbox replays it after a reconnect
ABORT_AFTER_S = 0.5  # the transport is aborted this long after a close started, if the peer has not answered
WRITE_TIMEOUT_S = 5.0  # a pong write that is not done by then (the peer does not read) aborts the connection
QUEUE_MAX = 1024  # frames waiting for one connection's writer; a full queue refuses the next frame (send -> False)
UPGRADES_PER_CERT_MAX = 8  # /ws upgrades in flight (not yet a WebSocket) per verified certificate fingerprint


INBOUND_BYTES_PER_S = 128 * 1024  # sustained inbound bytes per connection (a token bucket, see ``ByteBudget``)
INBOUND_BURST_BYTES = 128 * 1024  # the bucket's size: two 64 KiB frames back to back
INBOUND_MIN_CHARGE = 256  # every frame costs at least this (512 frames/s sustained), so tiny frames are bounded too


class ByteBudget:
    """A connection's inbound token bucket: ``allow(size)`` charges one received frame, False when it is over budget."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tokens = float(INBOUND_BURST_BYTES)
        self._updated = clock()

    def allow(self, size: int) -> bool:
        now = self._clock()
        self._tokens = min(INBOUND_BURST_BYTES, self._tokens + (now - self._updated) * INBOUND_BYTES_PER_S)
        self._updated = now
        cost = max(size, INBOUND_MIN_CHARGE)
        if cost > self._tokens:
            return False
        self._tokens -= cost
        return True


class TooManyUpgrades(Exception):
    """The key already has ``UPGRADES_PER_CERT_MAX`` upgrades in flight."""


class UpgradeCap:
    """Counts the ``/ws`` upgrades in flight per key (the verified certificate fingerprint); touched only on the event
    loop."""

    def __init__(self, per_key: int = UPGRADES_PER_CERT_MAX) -> None:
        self._per_key = per_key
        self._open: dict[str, int] = {}

    @contextmanager
    def slot(self, key: str) -> Iterator[None]:
        """Holds one upgrade slot of *key*; raises ``TooManyUpgrades`` on entry when the key is at the cap."""
        count = self._open.get(key, 0)
        if count >= self._per_key:
            raise TooManyUpgrades
        self._open[key] = count + 1
        try:
            yield
        finally:
            if self._open[key] <= 1:
                del self._open[key]  # the dict never outgrows the keys that are in flight right now
            else:
                self._open[key] -= 1


class Connection:
    """One device's WebSocket with its send queue and writer task; must be built on the running event loop."""

    def __init__(self, device_id: str, ws: web.WebSocketResponse, transport: asyncio.Transport | None) -> None:
        self.device_id, self.ws = device_id, ws
        self._transport = transport
        self._queue: asyncio.Queue[str] = asyncio.Queue(QUEUE_MAX)
        self._halted = self._closing = False
        self._ended = asyncio.Event()  # a close (or the abort of a blocked pong) has finished
        self._holds = 0  # nesting depth of ``reads_paused``
        self._closer: asyncio.Task[bool] | None = None
        self._ender: asyncio.Task[None] | None = None  # the close a refused frame started (``refused``)
        self._writer = asyncio.get_running_loop().create_task(self._write(), name="glasses-ws-writer")

    @property
    def pending(self) -> int:
        """Frames queued and not yet written."""
        return self._queue.qsize()

    def send(self, text: str) -> bool:
        """Queue one text message for the writer; False when the connection is ending or its queue is full."""
        if self._halted or self._queue.full():
            return False
        self._queue.put_nowait(text)
        return True

    def refused(self) -> None:
        """A frame ``send`` refused is never dropped silently: the connection ends with ``1008`` (the outbox keeps
        its rows for the replay after a reconnect); nothing is queued after it (``send`` refuses at once, so no later
        ``seq`` follows a gap); one DEBUG line when it is already ending."""
        self._halted = True
        if self._closing or self._ender is not None:
            _LOG.debug("glasses ws: frame not queued (connection ending)")
            return
        self._ender = asyncio.get_running_loop().create_task(self.close(CLOSE_POLICY), name="glasses-ws-refused")

    async def _write(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self.ws.send_str(text)
            except Exception as exc:  # noqa: BLE001 - a dead socket ends this writer; type only, the handler sees EOF
                _LOG.debug("glasses ws: writer ended (%s)", type(exc).__name__)
                self._halted = True
                return

    @contextmanager
    def reads_paused(self) -> Iterator[None]:
        """The socket is not read inside the block (nests: the outermost block resumes). The frame loop holds it for
        the whole time between "message taken" and the next ``receive()``, whatever that waits on (the yield, a pong
        write, ``on_frame``): a transport that kept reading would queue ~40 000 tiny frames per 256 KiB chunk, and
        aiohttp counts the queue by payload bytes, so empty messages are never limited by it. A ``close`` lifts it.

        Invariant: this calls the transport directly, not aiohttp's protocol (whose own ``_reading_paused`` flag stays
        untouched). That is safe because ``data_received`` runs only while the loop waits on an empty queue, and by
        then aiohttp has resumed itself: our resume can never lift a pause aiohttp set for its payload-byte limit."""
        transport = self._transport
        if self._holds == 0 and transport is not None and not transport.is_closing():
            transport.pause_reading()
        self._holds += 1
        try:
            yield
        finally:
            self._holds -= 1
            if self._holds == 0:
                self._resume_reads()

    def _resume_reads(self) -> None:
        transport = self._transport
        if transport is not None and not transport.is_closing():
            transport.resume_reading()

    async def yield_turn(self) -> None:
        """One turn of the event loop with the socket not read: the frame loop handles one frame per turn."""
        with self.reads_paused():
            await asyncio.sleep(0)

    async def pong(self, payload: bytes) -> bool:
        """Write a protocol pong; a write that has not finished after ``WRITE_TIMEOUT_S`` (the peer is not reading)
        aborts the connection, drops what is queued and returns False: it never parks the frame loop."""
        try:
            await asyncio.wait_for(self.ws.pong(payload), WRITE_TIMEOUT_S)
        except TimeoutError:
            self._closing = True  # no close frame: it could not be written either
            self._ended.set()  # nothing for a later close() to wait for
            await self.stop()
            self._abort()
            return False
        return True

    async def stop(self) -> None:
        """Stop accepting frames, drop what is queued and end the writer; writes nothing."""
        self._halted = True
        while not self._queue.empty():
            self._queue.get_nowait()
        self._writer.cancel()
        await asyncio.wait((self._writer,))  # waits without re-raising the writer's own end

    async def close(self, code: int) -> None:
        """``stop``, then the close frame; the transport is aborted ``ABORT_AFTER_S`` after this started. A second call
        waits for the first to end (a handler must not return before the first close frame is written)."""
        if self._closing:
            await self._ended.wait()
            return
        self._closing = True
        try:
            if self._holds:  # a frame is being handled with the socket unread: the peer's close frame must be read
                self._resume_reads()  # the abort timer bounds the wait
            loop = asyncio.get_running_loop()
            loop.call_later(ABORT_AFTER_S, self._abort)  # harmless on a transport that is closed by then
            await self.stop()
            self._closer = loop.create_task(self.ws.close(code=code), name="glasses-ws-closer")
            self._closer.add_done_callback(_retrieved)
            await asyncio.wait((self._closer,), timeout=ABORT_AFTER_S)  # a peer that never answers: the timer aborts
        finally:
            self._ended.set()

    def _abort(self) -> None:
        if self._transport is not None:
            self._transport.abort()


def _retrieved(done: asyncio.Task[bool]) -> None:
    """A close that failed because the peer is gone (or was aborted) is not an error nobody reads."""
    if not done.cancelled():
        done.exception()


class ConnectionTable:
    """The live connections by device id: at most one per device."""

    def __init__(self) -> None:
        self._by_device: dict[str, Connection] = {}
        self.closed = False  # set by close_all: a connection admitted from then on is closed, not kept

    async def admit(self, conn: Connection) -> bool:
        """Register *conn* as its device's connection and close the one it replaces with ``4409``.

        After ``close_all`` (shutdown) *conn* is closed with ``1001`` instead and not registered: False.
        """
        if self.closed:
            await conn.close(CLOSE_GOING_AWAY)
            return False
        old = self._by_device.get(conn.device_id)
        self._by_device[conn.device_id] = conn
        if old is not None:
            await old.close(CLOSE_REPLACED)
        return True

    def remove(self, conn: Connection) -> None:
        """Forget *conn* unless a newer connection of the device has taken its place."""
        if self._by_device.get(conn.device_id) is conn:
            del self._by_device[conn.device_id]

    def send(self, device_id: str, text: str) -> bool:
        """Queue a message for the device's current connection; False when it has none or its queue is full."""
        conn = self._by_device.get(device_id)
        return conn is not None and conn.send(text)

    async def close_device(self, device_id: str, code: int) -> bool:
        """Close the device's connection with *code* (queue discarded, transport aborted after 500 ms); False: none."""
        conn = self._by_device.pop(device_id, None)
        if conn is None:
            return False
        await conn.close(code)
        return True

    async def close_all(self, code: int = CLOSE_GOING_AWAY) -> None:
        """Close every connection at once (server shutdown); the table then refuses new ones (``admit``)."""
        self.closed = True
        conns = list(self._by_device.values())
        self._by_device.clear()
        await asyncio.gather(*(conn.close(code) for conn in conns))
