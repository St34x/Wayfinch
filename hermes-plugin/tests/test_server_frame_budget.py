# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b): one device's frames must not stall the gateway loop. The frame loop yields after each frame
and charges a per-connection byte budget before ``decode``; over it the connection is closed ``1008``."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any

import pytest
from server_helpers import app_for, serving, wiring
from server_raw_ws import TEXT, RawWs, frame_bytes, hello_bytes

from glasses import connections, server_session
from glasses.connections import ByteBudget

KIB = 1024
GAP_LIMIT = 0.25  # seconds: the loop-gap bound of the acceptance (before the fix 0.69-1.25 s were measured)
HUGE = 10**12


def ints_frame() -> bytes:
    """A 64 001-byte text frame ``[1,1,...]`` of 32 000 ints: valid JSON, an invalid envelope, 63-96 ms to refuse."""
    return frame_bytes(TEXT, b"[" + b",".join([b"1"] * 32_000) + b"]")


def ping_bytes() -> bytes:
    ping = b'{"v":1,"id":"01JABCDEFGHJKMNPQRSTVWXYZ0","type":"ping","ts":"2026-10-03T18:00:00Z","body":{}}'
    return frame_bytes(TEXT, ping)


class Flood:
    """What a flood of frames did: the probe's largest gap, the decodes done, the replies and the close code."""

    def __init__(self) -> None:
        self.max_gap = 0.0
        self.decoded = 0
        self.replies = 0
        self.close_code: int | None = None

    async def probe(self, stop: asyncio.Event) -> None:
        """A task ticking every 10 ms on the server's loop; the longest time between two ticks is the loop gap."""
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            self.max_gap, last = max(self.max_gap, now - last), now

    def client(self, port: int, headers: dict[str, str], frames: list[bytes]) -> None:
        """Hello, then *frames* back to back; reads replies until the server's close frame."""
        ws = RawWs(port, headers, timeout=20)
        try:
            ws.send(frame_bytes(TEXT, hello_bytes()))
            assert ws.recv() is not None  # the welcome: the session is live before the flood starts
            self.decoded = 0  # the hello does not count
            sender = threading.Thread(target=self._send_all, args=(ws, b"".join(frames)))
            sender.start()
            while (got := ws.recv()) is not None:
                if got[0] == 0x8:
                    self.close_code = int.from_bytes(got[1][:2], "big")
                    break
                self.replies += 1
            sender.join(20)
        finally:
            ws.shut()

    @staticmethod
    def _send_all(ws: RawWs, data: bytes) -> None:
        with contextlib.suppress(OSError):  # closed by the server in the middle of the flood
            ws.send(data)


def flood(tmp_path, monkeypatch, frames: list[bytes], *, budget: bool, frozen_clock: bool = False) -> Flood:
    """Send *frames* back to back on a live session (the budget lifted unless *budget*); a 10 ms probe task runs on
    the server's loop the whole time. *frozen_clock*: the bucket never refills (frames arrive faster than the
    server answers them; on a host slower than 512 pings/s the real clock refills as fast as they are answered)."""
    if not budget:
        monkeypatch.setattr(connections, "INBOUND_BYTES_PER_S", HUGE)
        monkeypatch.setattr(connections, "INBOUND_BURST_BYTES", HUGE)
    if frozen_clock:
        monkeypatch.setattr(server_session, "ByteBudget", lambda: ByteBudget(clock=lambda: 0.0))
    result, real_decode = Flood(), server_session.decode

    def counting_decode(data: Any, schema: Any) -> Any:
        result.decoded += 1
        return real_decode(data, schema)

    monkeypatch.setattr(server_session, "decode", counting_decode)
    w = wiring(tmp_path)

    async def main() -> None:
        stop = asyncio.Event()
        async with serving(app_for(w)) as port:
            ticking = asyncio.create_task(result.probe(stop))
            await asyncio.get_running_loop().run_in_executor(None, result.client, port, w.headers(), frames)
            stop.set()
            await ticking

    asyncio.run(asyncio.wait_for(main(), 60))
    return result


def test_twenty_back_to_back_64k_frames_do_not_hold_the_loop_longer_than_one_frame(tmp_path):
    gaps: list[float] = []
    for attempt in range(3):  # a loaded host adds gaps of its own (an idle probe alone saw 100 ms): best of three
        (tmp_path / str(attempt)).mkdir()
        with pytest.MonkeyPatch.context() as monkeypatch:
            result = flood(tmp_path / str(attempt), monkeypatch, [ints_frame()] * 20, budget=False)
        assert result.decoded == 20 and result.replies == 20
        gaps.append(result.max_gap)
        if result.max_gap < GAP_LIMIT:
            return
    pytest.fail("loop gaps in ms: " + ", ".join(f"{gap * 1000:.0f}" for gap in gaps))


def test_the_same_burst_under_the_default_budget_is_closed_1008_after_at_most_3_frames(tmp_path, monkeypatch):
    result = flood(tmp_path, monkeypatch, [ints_frame()] * 20, budget=True)
    assert result.close_code == 1008
    assert 1 <= result.decoded <= 3
    assert result.max_gap < GAP_LIMIT, f"loop gap {result.max_gap * 1000:.0f} ms"


def test_five_thousand_pings_sent_at_once_are_closed_1008_before_all_are_answered(tmp_path, monkeypatch):
    result = flood(tmp_path, monkeypatch, [ping_bytes()] * 5_000, budget=True, frozen_clock=True)
    assert result.close_code == 1008
    assert 480 <= result.decoded <= 512  # 128 KiB burst / 256 B minimum charge, less what the hello cost


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_budget_is_128_kib_burst_then_128_kib_per_second():
    clock = Clock()
    budget = ByteBudget(clock=clock)
    assert budget.allow(64 * KIB) and budget.allow(64 * KIB)  # the burst
    assert not budget.allow(64 * KIB)  # empty: the third frame of a back-to-back burst
    clock.now += 0.5
    assert budget.allow(64 * KIB) and not budget.allow(64 * KIB)  # half a second refills 64 KiB
    clock.now += 100  # idle time does not bank more than the burst
    assert budget.allow(128 * KIB) and not budget.allow(256)


def test_a_tiny_frame_is_charged_256_bytes_so_a_flood_of_them_is_bounded():
    clock = Clock()
    budget = ByteBudget(clock=clock)
    assert sum(budget.allow(1) for _ in range(5_000)) == 512
    clock.now += 1.0
    assert sum(budget.allow(1) for _ in range(5_000)) == 512  # sustained: 512 frames per second


def test_a_30_second_voice_clip_in_real_time_is_never_over_budget():
    """Worst case: raw 16 kHz s16le PCM (32 000 B/s) in the largest audio.chunk (31 744 base64 chars + envelope)."""
    clock = Clock()
    budget = ByteBudget(clock=clock)
    chunk, rate = 31_744 + 300, 32_000  # frame bytes; audio bytes per second
    interval = 23_808 / rate  # seconds of audio per frame
    for _ in range(int(30 / interval) + 1):
        assert budget.allow(chunk)
        clock.now += interval
    clock = Clock()
    budget = ByteBudget(clock=clock)  # 20 ms Opus frames (about 100 B + envelope): 50 frames/s, each charged 256 B
    for _ in range(30 * 50):
        assert budget.allow(400)
        clock.now += 0.02


@pytest.mark.parametrize("size", [0, 1, 255])
def test_a_frame_smaller_than_the_minimum_costs_the_minimum(size):
    budget = ByteBudget(clock=Clock())
    assert sum(budget.allow(size) for _ in range(1_000)) == 512
