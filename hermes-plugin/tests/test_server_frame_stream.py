# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07c): a SUSTAINED stream of tiny frames (empty pings, empty text frames) at line rate, and one huge
backlog of pings, close the socket ``1008`` without a multi-second loop stall and without hundreds of MB of queued
messages.

aiohttp parses a whole 256 KiB read into ~40 000 messages queued with no count limit, so the frame loop does not read
the transport while it yields between two frames (``Connection.yield_turn``): at most one chunk is ever queued ahead
of the loop, which also bounds the drain of the backlog when the close handshake runs."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time

import pytest
from server_helpers import app_for, serving, wiring
from server_raw_ws import CLOSE, TEXT, RawWs, frame_bytes, hello_bytes

from glasses import connections, server_session
from glasses.connections import ByteBudget

PING = 0x9
GAP_LIMIT = 2.0  # seconds; 0.1-1.4 s with the fix (the top at load 7 on 2 cores), 0.8-8 s without (RSS kills those)
RSS_GROWTH_LIMIT = 64 * 1024 * 1024  # bytes; 4-22 MB measured with the fix, 114 MB-1.25 GB without (load-independent)
QUIET_SECONDS_MAX = 10  # quiet client reads in a row, after the stream, before the client gives up on a close frame
STREAM_S = 3.0
CHUNK_FRAMES = 400_000  # one write of 2.4 MB (empty pings or empty text frames: 6 bytes each)
BACKLOG_FRAMES = 2_000_000  # one write of 12 MB


def _rss() -> int:
    """The resident set of this process now, in bytes."""
    with open("/proc/self/statm") as statm:
        return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


class Stream:
    """What a sustained stream did: the loop's largest gap, the peak resident-set growth and the close code."""

    def __init__(self, frames: int = CHUNK_FRAMES, once: bool = False) -> None:
        self.frames = frames
        self.once = once  # one write (a backlog) instead of writes for ``STREAM_S`` seconds
        self.max_gap = 0.0
        self.peak_growth = 0
        self.close_code: int | None = None
        self.sent = 0

    async def probe(self, stop: asyncio.Event) -> None:
        """Ticks every 10 ms on the server's loop; the longest time between two ticks is the loop gap."""
        base, last = _rss(), time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            self.max_gap, last = max(self.max_gap, now - last), now
            self.peak_growth = max(self.peak_growth, _rss() - base)

    def client(self, port: int, headers: dict[str, str], tiny: bytes) -> None:
        """Hello, then *tiny* frames back to back for ``STREAM_S`` seconds or until the server ends the session."""
        ws = RawWs(port, headers, timeout=20)
        try:
            ws.send(frame_bytes(TEXT, hello_bytes()))
            assert ws.recv() is not None  # the welcome: the session is live before the stream starts
            ws.sock.settimeout(1.0)
            sender = threading.Thread(target=self._send_all, args=(ws, tiny * self.frames))
            sender.start()
            quiet = 0
            while sender.is_alive() or self.close_code is None:
                got = ws.recv()  # None: a quiet second, a reset or the end of the connection
                quiet = 0 if got is not None else quiet + 1
                if got is None and not sender.is_alive() and quiet >= QUIET_SECONDS_MAX:
                    break  # a loaded host may need seconds to get to the close frame; EOF returns at once, ten times
                if got is not None and got[0] == CLOSE:
                    self.close_code = int.from_bytes(got[1][:2], "big")
            sender.join(20)
        finally:
            ws.shut()

    def _send_all(self, ws: RawWs, chunk: bytes) -> None:
        deadline = time.monotonic() + STREAM_S
        with contextlib.suppress(OSError):  # closed by the server in the middle of the stream
            while time.monotonic() < deadline:
                ws.send(chunk)
                self.sent += len(chunk)
                if self.once:
                    return


@pytest.mark.parametrize(
    "tiny", [frame_bytes(PING, b""), frame_bytes(TEXT, b"")], ids=["empty-pings", "empty-text-frames"]
)
def test_a_sustained_stream_of_tiny_frames_closes_1008_without_a_stall_or_a_memory_spike(tmp_path, monkeypatch, tiny):
    _assert_bounded(_run(tmp_path, monkeypatch, tiny, Stream()))


def test_a_huge_ping_backlog_closes_1008_without_a_stall(tmp_path, monkeypatch):
    """One write of 12 MB (2 000 000 pings): the drain of what aiohttp parsed ahead used to stall the close."""
    _assert_bounded(_run(tmp_path, monkeypatch, frame_bytes(PING, b""), Stream(BACKLOG_FRAMES, once=True)))


def _run(tmp_path, monkeypatch, tiny: bytes, result: Stream) -> Stream:
    """Serve one connection whose byte budget never refills: on a host that answers fewer than 512 frames/s (a loaded
    one) the real clock refills as fast as the frames are answered, the stream is never over budget and no ``1008``
    comes (the same freeze as in test_server_frame_budget.py). The abort timer is long too: the client in this process
    may need more than 0.5 s of a loaded host to read the close frame, and an abort first resets the connection."""
    monkeypatch.setattr(server_session, "ByteBudget", lambda: ByteBudget(clock=lambda: 0.0))
    monkeypatch.setattr(connections, "ABORT_AFTER_S", 10.0)
    w = wiring(tmp_path)

    async def main() -> None:
        stop = asyncio.Event()
        async with serving(app_for(w)) as port:
            ticking = asyncio.create_task(result.probe(stop))
            await asyncio.get_running_loop().run_in_executor(None, result.client, port, w.headers(), tiny)
            stop.set()
            await ticking

    asyncio.run(asyncio.wait_for(main(), 60))
    return result


def _assert_bounded(result: Stream) -> None:
    assert result.close_code == 1008
    assert result.max_gap < GAP_LIMIT, f"loop gap {result.max_gap * 1000:.0f} ms"
    assert result.peak_growth < RSS_GROWTH_LIMIT, f"RSS grew {result.peak_growth / 2**20:.0f} MiB"
