# SPDX-License-Identifier: Apache-2.0
"""A client for the blocked-write tests of the WS frame loop: it sends and NEVER reads, with a tiny receive buffer
(shrunk on the client only), so the server's pong writes stall once the kernel buffers between the two are full.

``watch_pongs`` tells the test, from inside the server's loop, when a ``pong`` write has been blocked for a while;
the client then floods with empty pings at line rate while the loop waits on that write, and afterwards checks that
the server ends the connection (a reset: the peer is not reading, so no close frame can be delivered)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web
from server_helpers import app_for, serving, wiring
from server_raw_ws import TEXT, RawWs, frame_bytes, hello_bytes
from server_stream_helpers import Stream

PING = 0x9
RCVBUF = 1024  # the client's SO_RCVBUF (the kernel doubles it and enforces a floor of 2304); never set on the server
PINGS_PER_S = 450  # 125-byte pings: under the 512 frames/s of the byte budget, 57 KB/s in, 57 KB/s of pongs out
BLOCKED_SEEN_S = 1.0  # a pong write this long unfinished counts as blocked
BLOCK_WAIT_MAX_S = 150.0  # the client waits this long for the first blocked pong write (about 25-50 s measured)
FLOOD_S = 2.0  # line-rate empty pings while the write is blocked: +382 MiB without a read pause
FLOOD_CHUNK_FRAMES = 100_000  # one send of 600 KB


def watch_pongs(patch: pytest.MonkeyPatch, seen: threading.Event) -> None:
    """Set *seen* when a ``WebSocketResponse.pong`` write has been unfinished for ``BLOCKED_SEEN_S`` seconds."""
    original = web.WebSocketResponse.pong

    async def pong(self: web.WebSocketResponse, message: bytes = b"") -> None:
        timer = asyncio.get_running_loop().call_later(BLOCKED_SEEN_S, seen.set)
        try:
            await original(self, message)
        finally:
            timer.cancel()

    patch.setattr(web.WebSocketResponse, "pong", pong)


class NeverReader:
    """What a never-reading client saw: when the write blocked, whether the server ended the connection, and when."""

    def __init__(self, blocked: threading.Event, close_bound_s: float) -> None:
        self.blocked, self.close_bound_s = blocked, close_bound_s
        self.blocked_at: float | None = None
        self.reset_at: float | None = None

    @property
    def closed_after(self) -> float | None:
        """Seconds from the first blocked write to the reset, ``None`` if the write never blocked or no reset came."""
        if self.blocked_at is None or self.reset_at is None:
            return None
        return self.reset_at - self.blocked_at

    def client(self, port: int, headers: dict[str, str]) -> None:
        ws = RawWs(port, headers, timeout=20, rcvbuf=RCVBUF)
        try:
            ws.send(frame_bytes(TEXT, hello_bytes()))
            assert ws.recv() is not None  # the welcome; nothing is read after it
            ws.sock.settimeout(1.0)
            self._until_blocked(ws.sock)
            if self.blocked_at is not None:
                self._flood(ws.sock)
                self._wait_for_reset(ws.sock)
        finally:
            ws.shut()

    def _send(self, sock: socket.socket, data: bytes) -> bool:
        """False when the send did not complete: the server reset the connection (the time is kept) or its receive
        buffer is full (it is not reading: paused or parked, not closed)."""
        try:
            sock.sendall(data)
        except (ConnectionResetError, BrokenPipeError):
            self.reset_at = self.reset_at or time.monotonic()
            return False
        except TimeoutError:
            return False
        return True

    def _until_blocked(self, sock: socket.socket) -> None:
        ping, start = frame_bytes(PING, b"x" * 125), time.monotonic()
        count = 0
        while time.monotonic() - start < BLOCK_WAIT_MAX_S:
            if self.blocked.is_set():
                self.blocked_at = time.monotonic()
                return
            if not self._send(sock, ping) and self.reset_at is not None:
                return
            count += 1
            time.sleep(max(0.0, start + count / PINGS_PER_S - time.monotonic()))

    def _flood(self, sock: socket.socket) -> None:
        chunk, end = frame_bytes(PING, b"") * FLOOD_CHUNK_FRAMES, time.monotonic() + FLOOD_S
        while time.monotonic() < end and self._send(sock, chunk):
            pass

    def _wait_for_reset(self, sock: socket.socket) -> None:
        assert self.blocked_at is not None
        ping = frame_bytes(PING, b"")
        while self.reset_at is None and time.monotonic() - self.blocked_at < self.close_bound_s:
            self._send(sock, ping)
            time.sleep(0.1)


def run_never_reader(tmp_path: Path, reader: NeverReader, stream: Stream) -> None:
    """Serve one connection (the real byte budget and abort timer) to *reader*; *stream* measures the server's loop."""
    w = wiring(tmp_path)

    async def main() -> None:
        stop = asyncio.Event()
        async with serving(app_for(w)) as port:
            ticking = asyncio.create_task(stream.probe(stop))
            await asyncio.get_running_loop().run_in_executor(None, reader.client, port, w.headers())
            stop.set()
            await ticking

    with pytest.MonkeyPatch.context() as patch:
        watch_pongs(patch, reader.blocked)
        asyncio.run(asyncio.wait_for(main(), 240))
