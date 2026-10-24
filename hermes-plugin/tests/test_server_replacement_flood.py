# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.184b): a device reconnects while its old connection is busy and ignores the close
frame, and the new connection floods 6-byte frames at line rate from its first byte. ``admit`` waits for the old
close (up to ``ABORT_AFTER_S``): the new socket must stay unread for that wait too, or aiohttp queues the empty frames
without a count limit and the close then drains them in one go. Same bounds as line 16: loop gap under 250 ms,
resident set growth under 100 MiB, and the flood ends in a ``1008``.

The old connection must be busy in ``on_frame``: a handler that waits in ``receive()`` is closed by aiohttp the moment
its close frame is written, so ``admit`` returns at once. Only a handler that is not reading makes the close wait for
the peer's answer, and the old peer never answers."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable

from server_helpers import MSG_IN
from server_raw_ws import CLOSE, TEXT, RawWs, frame_bytes, hello_bytes
from server_stream_helpers import CHUNK, PING, Stream, run_stream

from glasses.connections import Connection, ConnectionTable

from test_server_frame_stream_gaps import FREE_GAP_LIMIT, GROWTH_LIMIT

ABORT_AFTER_S = 2.0  # the wait for the old connection's close: long enough that an unread socket would show it
DISPATCH_HOLD_S = 4.0  # the old connection's handler stays inside on_frame this long (longer than the wait)


class ReplacementFlood(Stream):
    """Connects the device (the old connection: it sends one frame that holds its handler in ``on_frame``, then never
    reads or answers), connects it again and floods from the new connection's first byte, before the server could
    have admitted it."""

    def __init__(self, tiny: bytes, entered: threading.Event) -> None:
        super().__init__(tiny * (3 * CHUNK))
        self.entered = entered
        self.old_close: int | None = None

    def client(self, port: int, headers: dict[str, str], before: Callable[[RawWs], None] | None = None) -> None:
        old = RawWs(port, headers, timeout=20)
        try:
            old.send(frame_bytes(TEXT, hello_bytes()))
            assert old.recv() is not None  # the welcome: the old connection is registered
            old.send(frame_bytes(TEXT, json.dumps(MSG_IN).encode()))
            assert self.entered.wait(10), "the old connection never reached on_frame"
            new = RawWs(port, headers, timeout=20)
            try:
                new.send(frame_bytes(TEXT, hello_bytes()))  # no welcome is awaited: admit is still waiting
                self.holding = True  # the loop's gap is measured from here: start-up gaps are not the flood's
                self.flood(new)
                self.holding = False
            finally:
                new.shut()
            got = old.recv()  # replaced and closed by the server: the close frame or a reset, never a hang
            self.old_close = int.from_bytes(got[1][:2], "big") if got is not None and got[0] == CLOSE else None
        finally:
            old.shut()


def test_a_reconnect_with_a_slow_closing_old_peer_and_an_empty_frame_flood_stays_bounded(tmp_path, monkeypatch):
    entered, waits = threading.Event(), []
    real_admit = ConnectionTable.admit

    async def timed_admit(self: ConnectionTable, conn: Connection) -> bool:
        started = time.monotonic()
        try:
            return await real_admit(self, conn)
        finally:
            waits.append(time.monotonic() - started)

    async def slow_dispatch(device: object, frame: object) -> None:
        entered.set()
        await asyncio.sleep(DISPATCH_HOLD_S)

    monkeypatch.setattr(ConnectionTable, "admit", timed_admit)
    flood = ReplacementFlood(frame_bytes(PING, b""), entered)
    run_stream(tmp_path, flood, abort_after=ABORT_AFTER_S, on_frame=slow_dispatch)
    assert max(waits) >= ABORT_AFTER_S * 0.8, f"admit waited {max(waits):.2f} s: the old close was not slow"
    assert flood.close_code == 1008
    assert flood.held_gap < FREE_GAP_LIMIT, f"loop gap {flood.held_gap * 1000:.0f} ms during the flood"
    assert flood.peak_growth < GROWTH_LIMIT, f"RSS grew {flood.peak_growth / 2**20:.0f} MiB"
    assert flood.old_close in (4409, None)  # the old connection ended: replaced (4409) or already reset
