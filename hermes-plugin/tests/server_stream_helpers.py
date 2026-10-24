# SPDX-License-Identifier: Apache-2.0
"""Shared helpers of the WS frame-stream tests: a sender thread that writes one prepared byte string over and over (or
once) at line rate, a loop-gap and resident-set probe on the server's own loop, and the close code the client saw."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from server_helpers import Wiring, app_for, serving, wiring
from server_raw_ws import CLOSE, TEXT, RawWs, frame_bytes, hello_bytes

from glasses import connections, server_session
from glasses.connections import ByteBudget

PING, PONG = 0x9, 0xA
GAP_LIMIT = 2.0  # seconds the server's loop may be held in one go; 0.1-0.35 s idle, up to 1.4 s at load 7 on 2 cores
QUIET_SECONDS_MAX = 10  # quiet client reads in a row, after the stream, before the client gives up on a close frame
RSS_GROWTH_LIMIT = 150 * 1024 * 1024  # bytes of resident set a stream may add
CHUNK = 400_000  # frames per write of an empty-frame stream (6 bytes each)


def rss() -> int:
    """The resident set of this process now, in bytes."""
    with open("/proc/self/statm") as statm:
        return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


class Stream:
    """What a stream did to the server's loop: its largest gap, peak resident-set growth, ticks and the close code."""

    def __init__(
        self, chunk: bytes, seconds: float = 3.0, once: bool = False, patience: int = QUIET_SECONDS_MAX
    ) -> None:
        self.chunk, self.seconds, self.once, self.patience = chunk, seconds, once, patience
        self.max_gap = 0.0
        self.peak_growth = 0
        self.ticks = 0
        self.holding = False  # set by a test while its dispatch stub waits: the loop is free then, so its gap is small
        self.held_gap = 0.0
        self.close_code: int | None = None
        self.sent = 0
        self.wall = 0.0

    async def probe(self, stop: asyncio.Event) -> None:
        """Ticks every 10 ms on the server's loop; the longest time between two ticks is the loop gap."""
        base, last = rss(), time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            last_tick = last
            self.max_gap, last = max(self.max_gap, now - last), now
            self.ticks += 1
            if self.holding:
                self.held_gap = max(self.held_gap, now - last_tick)
            self.peak_growth = max(self.peak_growth, rss() - base)

    def client(self, port: int, headers: dict[str, str], before: Callable[[RawWs], None] | None = None) -> None:
        """Hello, then the chunk back to back for ``seconds`` or until the server ends the session."""
        ws = RawWs(port, headers, timeout=20)
        try:
            ws.send(frame_bytes(TEXT, hello_bytes()))
            assert ws.recv() is not None  # the welcome: the session is live before the stream starts
            if before is not None:
                before(ws)
            self.flood(ws)
        finally:
            ws.shut()

    def flood(self, ws: RawWs) -> None:
        """The chunk back to back for ``seconds`` (or until the server ends the session), then wait for the close."""
        ws.sock.settimeout(1.0)
        sender = threading.Thread(target=self._send_all, args=(ws,))
        sender.start()
        quiet = 0
        while sender.is_alive() or self.close_code is None:
            got = ws.recv()  # None: a quiet second, a reset or the end of the connection
            quiet = 0 if got is not None else quiet + 1
            if got is None and not sender.is_alive() and quiet >= self.patience:
                break  # a loaded host may need seconds to get to the close frame; an EOF returns at once, ten times
            if got is not None and got[0] == CLOSE:
                self.close_code = int.from_bytes(got[1][:2], "big")
        sender.join(20)

    def _send_all(self, ws: RawWs) -> None:
        deadline = time.monotonic() + self.seconds
        with contextlib.suppress(OSError):  # closed by the server in the middle of the stream
            while time.monotonic() < deadline:
                ws.send(self.chunk)
                self.sent += len(self.chunk)
                if self.once:
                    return


def run_stream(
    tmp_path: Path,
    stream: Stream,
    before: Callable[[RawWs], None] | None = None,
    abort_after: float = 10.0,
    **changes: Any,
) -> tuple[Stream, Wiring]:
    """Run *stream* against the real app; ``changes`` replace parts of its ``ServerDeps``.

    The byte budget never refills and the abort timer (*abort_after* seconds) is long: on a host that answers fewer
    than 512 frames/s (a loaded one) the real clock refills as fast as the frames are answered, the stream is never
    over budget and no ``1008`` comes; a reset before the client read the close frame loses the code (the same freeze
    as test_server_frame_stream)."""
    w = wiring(tmp_path)

    async def main() -> None:
        stop = asyncio.Event()
        async with serving(app_for(w, **changes)) as port:
            ticking = asyncio.create_task(stream.probe(stop))
            started = time.monotonic()
            await asyncio.get_running_loop().run_in_executor(None, stream.client, port, w.headers(), before)
            stream.wall = time.monotonic() - started
            stop.set()
            await ticking

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server_session, "ByteBudget", lambda: ByteBudget(clock=lambda: 0.0))
        patch.setattr(connections, "ABORT_AFTER_S", abort_after)
        asyncio.run(asyncio.wait_for(main(), 60))
    return stream, w


def assert_closed_and_bounded(result: Stream) -> None:
    assert result.close_code == 1008
    assert result.max_gap < GAP_LIMIT, f"loop gap {result.max_gap * 1000:.0f} ms"
    assert result.peak_growth < RSS_GROWTH_LIMIT, f"RSS grew {result.peak_growth / 2**20:.0f} MiB"
