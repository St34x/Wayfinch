# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07c): frame mixes other than empty pings and empty text frames at line rate (binary, pongs, fragments,
close frames, max-size frames among tiny ones) end in a close without a loop stall or a memory spike, while real-time
voice traffic and a reader that is slow to read the pongs are not disturbed by the pause around each frame."""

from __future__ import annotations

import json
import time

import pytest
from server_helpers import wiring
from server_raw_ws import BINARY, CLOSE, CONTINUATION, TEXT, RawWs, frame_bytes, say_hello, session
from server_stream_helpers import CHUNK, GAP_LIMIT, PING, PONG, Stream, assert_closed_and_bounded, run_stream

APP_PING = json.dumps({
    "v": 1,
    "id": "01JABCDEFGHJKMNPQRSTVWXYZ0",
    "type": "ping",
    "ts": "2026-10-03T18:00:00Z",
    "body": {},
}).encode()
BIG = frame_bytes(TEXT, b"x" * 65_536)  # the largest message the server accepts, not valid JSON

MIXES = {
    "empty-binary": frame_bytes(BINARY, b"") * CHUNK,
    "empty-pongs": frame_bytes(PONG, b"") * CHUNK,
    "ping-text-binary-pong": (
        frame_bytes(PING, b"") + frame_bytes(TEXT, b"") + frame_bytes(BINARY, b"") + frame_bytes(PONG, b"")
    )
    * (CHUNK // 4),
    "empty-fragments": (frame_bytes(TEXT, b"", fin=False) + frame_bytes(CONTINUATION, b"", fin=True)) * (CHUNK // 2),
    "pings-between-fragments": frame_bytes(TEXT, b"", fin=False)
    + (frame_bytes(CONTINUATION, b"", fin=False) + frame_bytes(PING, b"")) * (CHUNK // 2),
    "full-pings": frame_bytes(PING, b"p" * 125) * (CHUNK // 10),
}


@pytest.mark.parametrize("chunk", MIXES.values(), ids=MIXES.keys())
def test_a_sustained_stream_of_a_frame_mix_closes_1008_without_a_stall_or_a_memory_spike(tmp_path, chunk):
    result, w = run_stream(tmp_path, Stream(chunk))
    assert_closed_and_bounded(result)
    assert w.frames == []  # nothing reached the dispatch seam


def test_max_size_frames_among_tiny_ones_close_1008_at_once(tmp_path):
    result, _ = run_stream(tmp_path, Stream((BIG + frame_bytes(PING, b"") * 50) * 20))
    assert_closed_and_bounded(result)
    assert result.wall < 5, f"the session took {result.wall:.1f} s to end"


def test_a_flood_of_close_frames_ends_the_session_with_the_peers_code(tmp_path):
    result, _ = run_stream(tmp_path, Stream(frame_bytes(CLOSE, b"\x03\xe8") * (CHUNK // 4)))
    assert result.close_code in (1000, 1008)
    assert result.max_gap < GAP_LIMIT and result.wall < 5


def test_real_time_voice_traffic_is_not_closed_and_the_session_stays_usable(tmp_path):
    """50 frames/s of Opus size and the largest audio.chunk every 0.74 s, for 3 s: well inside the budget."""
    opus, chunk = frame_bytes(TEXT, b"o" * 400), frame_bytes(TEXT, b"c" * 31_744)

    def scenario(ws: RawWs) -> int:
        say_hello(ws)
        start, sent, next_chunk = time.monotonic(), 0, 0.0
        while (now := time.monotonic() - start) < 3.0:
            ws.send(opus)
            if now >= next_chunk:
                ws.send(chunk)
                next_chunk += 0.744
            sent += 1
            time.sleep(max(0.0, start + sent * 0.02 - time.monotonic()))
        ws.send(frame_bytes(TEXT, APP_PING))
        while (got := ws.recv()) is not None:
            assert got[0] != CLOSE, got
            if got[0] == TEXT and b'"pong"' in got[1]:
                return sent
        raise AssertionError("no pong: the session ended")

    assert session(wiring(tmp_path), scenario) >= 140


def test_a_client_that_reads_its_pongs_late_gets_every_one_in_order(tmp_path):
    """The pause around each frame must not wait on the writer: 300 pings, nothing read for 1.5 s, then all pongs."""

    def scenario(ws: RawWs) -> list[bytes]:
        say_hello(ws)
        ws.send(b"".join(frame_bytes(PING, b"%03d" % i + b"x" * 122) for i in range(300)))
        time.sleep(1.5)
        pongs: list[bytes] = []
        while len(pongs) < 300 and (got := ws.recv()) is not None:
            assert got[0] == PONG, got
            pongs.append(got[1])
        return pongs

    pongs = session(wiring(tmp_path), scenario)
    assert [p[:3] for p in pongs] == [b"%03d" % i for i in range(300)]
