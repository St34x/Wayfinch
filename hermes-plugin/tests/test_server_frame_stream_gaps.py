# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07c): what a connected device can still do to the gateway loop within or around the inbound byte
budget. Each test states the bound a stream should keep; the one that does not is a known gap (strict xfail: it
fails the suite when fixed, so the marker is removed with the fix). The frame loop must be safe while ANY await of it
blocks (T1.184b): a slow dispatch, a pong write to a client that does not read."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from server_blocked_write import NeverReader, run_never_reader
from server_helpers import MSG_IN
from server_raw_ws import CONTINUATION, TEXT, RawWs, frame_bytes
from server_stream_helpers import CHUNK, PING, Stream, assert_closed_and_bounded, run_stream

from glasses.connections import WRITE_TIMEOUT_S

DISPATCH_HOLD_S = 2.5  # how long the dispatch seam of the stalled-handler test waits for one frame
GROWTH_LIMIT = 100 * 1024 * 1024  # bytes of resident set the server may add while one of its awaits is blocked
FREE_GAP_LIMIT = 0.25  # seconds; the longest the loop may be held while it waits (the bound of F-3)
CLOSE_BOUND_S = WRITE_TIMEOUT_S + 10.0  # a blocked pong write ends in a reset within this long after it was seen


@pytest.mark.xfail(
    reason="known gap: empty continuation frames never complete a message, so they are never charged to the byte "
    "budget and the connection is neither closed nor slowed; the parser spends most of one core on them",
    strict=True,
    raises=AssertionError,
)
def test_a_flood_of_empty_continuation_frames_closes_1008(tmp_path):
    flood = frame_bytes(TEXT, b"", fin=False) + frame_bytes(CONTINUATION, b"", fin=False) * CHUNK
    assert_closed_and_bounded(run_stream(tmp_path, Stream(flood, seconds=2.0, patience=1))[0])


@pytest.mark.parametrize(
    "tiny", [frame_bytes(PING, b""), frame_bytes(TEXT, b"")], ids=["empty-pings", "empty-text-frames"]
)
def test_a_flood_during_a_slow_dispatch_does_not_grow_the_resident_set(tmp_path, tiny):
    """``on_frame`` blocks 2.5 s while the peer streams 6-byte frames: the socket stays unread for the whole time, the
    loop is free (gap under 250 ms), the resident set grows under 100 MiB and the flood ends in a ``1008``."""
    flood = Stream(tiny * (3 * CHUNK), once=True)

    async def slow_dispatch(device: object, frame: object) -> None:
        flood.holding = True
        await asyncio.sleep(DISPATCH_HOLD_S)
        flood.holding = False

    def send_one_frame(ws: RawWs) -> None:
        ws.send(frame_bytes(TEXT, json.dumps(MSG_IN).encode()))  # the loop is now inside the dispatch seam

    run_stream(tmp_path, flood, before=send_one_frame, on_frame=slow_dispatch)
    assert flood.close_code == 1008
    assert flood.held_gap < FREE_GAP_LIMIT, f"loop gap {flood.held_gap * 1000:.0f} ms while dispatch waited"
    assert flood.peak_growth < GROWTH_LIMIT, f"RSS grew {flood.peak_growth / 2**20:.0f} MiB"


def test_a_flood_during_a_blocked_pong_write_does_not_grow_the_resident_set_and_ends_in_a_reset(tmp_path):
    """A client with a tiny receive buffer sends 125-byte pings at 450/s and never reads: after a while the pong write
    blocks (measured: about 25 s). Then it floods empty pings at line rate for 2 s. The socket stays unread (under
    100 MiB growth, loop gap under 250 ms) and the write that does not finish within ``WRITE_TIMEOUT_S`` aborts the
    connection: the client is reset within ``WRITE_TIMEOUT_S`` + 10 s of the first blocked write."""
    reader, stream = NeverReader(threading.Event(), CLOSE_BOUND_S), Stream(b"")
    run_never_reader(tmp_path, reader, stream)
    assert reader.blocked_at is not None, "the pong write never blocked: the client did not fill the buffers"
    assert reader.closed_after is not None, f"no reset within {CLOSE_BOUND_S:.0f} s of the blocked write"
    assert stream.max_gap < FREE_GAP_LIMIT, f"loop gap {stream.max_gap * 1000:.0f} ms"
    assert stream.peak_growth < GROWTH_LIMIT, f"RSS grew {stream.peak_growth / 2**20:.0f} MiB"
