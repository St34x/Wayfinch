# SPDX-License-Identifier: Apache-2.0
"""The session of one authenticated ``/ws`` connection (step 6 of ``server``'s ``/ws`` handler; moved out of
``server.py`` unchanged, T1.249). Its records keep ``server``'s logger name.

A message of up to 65 536 bytes (``MAX_MSG_SIZE``) is accepted, a larger one closes ``1009``. The first frame must be
``hello``, answered by ``welcome`` (``maxAudioSec`` = the configured clip cap, at most 30; the device's lanes, at most
32); ``ping`` is answered by ``pong``. Every frame passes ``decode``, the per-frame revocation check, then
``deps.on_frame(device, frame)``; a rejected frame gets the codec's ``error`` frame (no echo) and the session stays up.
A frame that meets a re-read of the check's data is held until that read ends (``server_auth.revoked_now``; a second
change of the file during that read: dropped). A frame of a device revoked in the deny overlay or by the signer (the
injected check, ``revoked_ids``, whose poll also cuts an idle session) is dropped and the device closed ``4403`` (one
INFO line with its id); a frame whose check fails is dropped, undispatched and unacknowledged. A ``hello`` that cannot
be checked (that check fails, or ``on_hello`` raises ``TryAgainLater``) closes the session ``1013`` before ``welcome``;
a failing ``on_frame`` or ``on_hello`` ``1011``. One frame is handled per turn of the loop, and the socket is not read
from the moment a message is taken until the loop asks for the next one (``Connection.reads_paused``), whatever the
handling waits on: at most one 256 KiB read, ~40 000 tiny frames, is parsed ahead of the loop. A pong write not done
after 5 s (``connections.WRITE_TIMEOUT_S``: the peer does not read) aborts the connection; it never parks the loop.
Held by tests: a client with a tiny ``SO_RCVBUF`` that never reads (125-byte pings at 450/s until the pong write
blocks, then line-rate empty pings for 2 s) is reset within 15 s of the blocked write, and ``on_frame`` blocked for
2.5 s under a stream of 6-byte pings or empty text frames ends in ``1008``; both with resident-set growth under
100 MiB and a loop gap under 250 ms. An ``on_frame`` that never returns holds its connection (unread) while it runs.
Each connection has an inbound byte budget (``connections.ByteBudget``: 128 KiB/s, burst 128 KiB, each frame charged
at least 256 bytes, on the received bytes before ``decode``): a frame over it closes ``1008``, undecoded and
undispatched (measured: loop gap about 0.1-0.35 s on an idle 2-core host, memory growth under 25 MB). WebSocket
protocol pings (opcode 9) count as frames: autoping is off, the loop charges each one (payload, at least 256 bytes),
yields and answers it with a pong of the same payload; a pong from the device is ignored.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from aiohttp import WSCloseCode, WSMessage, WSMsgType, web

from .config import MAX_LANES
from .connections import ByteBudget, Connection
from .envelope import FrameRejected, InvalidFrame, decode, error_frame
from .envelope_encode import encode
from .generated.protocol_types import Frame, HelloFrame, WelcomeFrame
from .registry import Device
from .server_auth import AuthDeps, TryAgainLater, note_revoked, revoked_now

if TYPE_CHECKING:  # annotations only: server imports this module
    from .server import ServerDeps

_LOG = logging.getLogger(__name__.removesuffix("_session"))  # server's logger: the records keep their name

MAX_MSG_SIZE = 65_536  # bytes; also welcome.limits.maxFrame and the codec's cap: the largest message accepted
_CONTROL = (WSMsgType.PING, WSMsgType.PONG)  # autoping is off: the frame loop sees, charges and answers these
_DATA_AND_CONTROL = (WSMsgType.TEXT, WSMsgType.BINARY, *_CONTROL)  # any other message type ends the session
MAX_SEQ = 2**53 - 1  # the schema maximum of a seq number
CLOSE_REVOKED = 4403
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    number = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10))
    return "".join(_CROCKFORD[(number >> shift) & 31] for shift in range(125, -1, -5))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _welcome(device: Device, resume: int, max_audio_sec: int) -> WelcomeFrame:
    now = _now()
    body = {
        "deviceId": device.device_id,
        "serverTime": now,
        "lanes": list(device.lanes[:MAX_LANES]),
        "limits": {"maxFrame": MAX_MSG_SIZE, "maxAudioSec": max_audio_sec},
        "resumeFrom": resume,
    }
    return cast(WelcomeFrame, {"v": 1, "id": _ulid(), "type": "welcome", "ts": now, "body": body})


def _reply(conn: Connection, frame: object, deps: ServerDeps) -> None:
    """Queue a frame for the connection's writer; a refused one ends the connection (``Connection.refused``)."""
    if not conn.send(encode(cast(Frame, frame), deps.schema)):
        conn.refused()


def _size(data: str | bytes | bytearray) -> int:
    """The bytes a received message costs: a text message by its UTF-8 size, a binary or control payload as it is."""
    return len(data.encode()) if isinstance(data, str) else len(data)


def _parse(data: str | bytes, deps: ServerDeps, welcomed: bool) -> Frame:
    """Decode one message; a hello is valid exactly once, as the first frame (``InvalidFrame`` otherwise)."""
    frame = decode(data, deps.schema)
    if (frame["type"] == "hello") == welcomed:
        raise InvalidFrame
    return frame


class _Session:
    """The frame loop's state for one connection: whether ``hello`` was answered and the inbound byte budget."""

    def __init__(self, conn: Connection, device: Device, deps: ServerDeps, auth: AuthDeps) -> None:
        self.conn, self.device, self.deps, self.auth = conn, device, deps, auth
        self.welcomed = False
        self.budget = ByteBudget()

    async def handle(self, message: WSMessage) -> bool:
        """Handle one message; False ends the session. The caller holds the read pause for the whole call."""
        conn = self.conn
        if not self.budget.allow(_size(message.data)):
            await conn.close(WSCloseCode.POLICY_VIOLATION)  # 1008: over the byte budget; not decoded, not dispatched
            return False
        await conn.yield_turn()  # one frame per turn of the loop
        if message.type in _CONTROL:  # a protocol ping is answered with its payload (a bounded write); a pong ignored
            return message.type is not WSMsgType.PING or await conn.pong(bytes(message.data))
        try:
            frame = _parse(message.data, self.deps, self.welcomed)
        except FrameRejected as rejected:
            _reply(conn, error_frame(rejected, _ulid(), _now()), self.deps)
            return True
        try:
            revoked = await revoked_now(self.auth, self.device.device_id)  # held while the check's data is re-read
        except web.HTTPException:  # the check failed (logged): fail closed, drop the frame, no acknowledgement
            if not self.welcomed:  # a hello that cannot be checked: a clean close (1013), never a session left hanging
                await conn.close(WSCloseCode.TRY_AGAIN_LATER)
            return self.welcomed
        if revoked:
            _LOG.info("glasses ws: frame of a revoked device; closing 4403 (deviceId=%s)", self.device.device_id)
            note_revoked(self.auth, self.device.device_id)
            await conn.close(CLOSE_REVOKED)
            return False
        try:
            await self._dispatch(frame)
        except TryAgainLater:  # on_hello could not check the device (logged there): never welcomed, a clean 1013
            await conn.close(WSCloseCode.TRY_AGAIN_LATER)
            return False
        return True

    async def _dispatch(self, frame: Frame) -> None:
        if frame["type"] == "hello":
            await self._hello(frame)
        elif frame["type"] == "ping":
            _reply(self.conn, {"v": 1, "id": _ulid(), "type": "pong", "ts": _now(), "body": {}}, self.deps)
        else:
            await self.deps.on_frame(self.device, frame)

    async def _hello(self, hello: HelloFrame) -> None:
        """``welcome``, then the replayed frames in ``seq`` order, queued with no await in between (no live frame
        of the outbox can come first)."""
        acked, replay = hello["body"]["lastAckedSeq"], list[str]()
        resume = min(acked + 1, MAX_SEQ)  # the schema maximum: no overflow at 2**53 - 1
        if self.deps.on_hello is not None:
            resume, replay = await self.deps.on_hello(self.device.device_id, acked, self.conn)
        _reply(self.conn, _welcome(self.device, resume, self.deps.max_audio_sec), self.deps)
        if not all(self.conn.send(text) for text in replay):
            self.conn.refused()
        self.welcomed = True


async def _frames(conn: Connection, device: Device, deps: ServerDeps, auth: AuthDeps) -> None:
    """The frame loop of one connection; returns when the socket ends or the device is dropped. The socket is not
    read from "message taken" to the next ``receive()``, whatever the handling waits on. ``deps.on_closed`` is called
    on every exit, a raising one included (the outbox forgets the connection)."""
    try:
        await _loop(_Session(conn, device, deps, auth))
    finally:
        if deps.on_closed is not None:
            deps.on_closed(device.device_id, conn)


async def _loop(session: _Session) -> None:
    conn = session.conn
    async for message in conn.ws:
        if message.type not in _DATA_AND_CONTROL:
            break
        with conn.reads_paused():
            if not await session.handle(message):
                return
