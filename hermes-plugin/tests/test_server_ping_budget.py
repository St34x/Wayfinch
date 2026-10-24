# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07c): WebSocket protocol pings (opcode 9) are charged to the connection's byte budget and answered by
the server's frame loop, not by aiohttp's autoping, so a flood of them closes the socket ``1008`` and cannot stall the
gateway loop, while a single legitimate ping still gets its pong."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

from server_helpers import app_for, serving, wiring
from server_raw_ws import TEXT, RawWs, frame_bytes, hello_bytes, reply, say_hello, session

from glasses import server_session
from glasses.connections import ByteBudget

PING, PONG = 0x9, 0xA
GAP_LIMIT = 0.25  # seconds: the loop-gap bound of the frame-budget acceptance
FLOOD = 20_000  # empty pings: 6 bytes each on the wire, 120 KB; 829 ms of one stalled loop turn before the fix
APP_PING = b'{"v":1,"id":"01JABCDEFGHJKMNPQRSTVWXYZ0","type":"ping","ts":"2026-10-03T18:00:00Z","body":{}}'


def test_one_ping_on_a_live_session_gets_a_pong_with_the_same_payload(tmp_path):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> tuple[int, bytes] | None:
        assert say_hello(ws)["type"] == "welcome"
        ws.send(frame_bytes(PING, b"keepalive-1"))
        return ws.recv()

    assert session(w, scenario) == (PONG, b"keepalive-1")


def test_an_unsolicited_pong_is_ignored_and_the_session_goes_on(tmp_path):
    w = wiring(tmp_path)

    def scenario(ws: RawWs) -> tuple[str, str]:
        say_hello(ws)
        ws.send(frame_bytes(PONG, b"x"))
        ws.send(frame_bytes(PING, b""))
        answered = ws.recv()
        ws.send(frame_bytes(TEXT, APP_PING))
        return ("pong" if answered == (PONG, b"") else str(answered), reply(ws)["type"])

    assert session(w, scenario) == ("pong", "pong")  # the protocol pong first, then the app-level pong frame


class PingFlood:
    """What a flood of protocol pings did: the probe's largest gap, the pongs read and the close code."""

    def __init__(self) -> None:
        self.max_gap = 0.0
        self.pongs = 0
        self.close_code: int | None = None

    async def probe(self, stop: asyncio.Event) -> None:
        """A task ticking every 10 ms on the server's loop; the longest time between two ticks is the loop gap."""
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            self.max_gap, last = max(self.max_gap, now - last), now

    def client(self, port: int, headers: dict[str, str], count: int) -> None:
        """Hello, then *count* empty pings back to back; reads replies until the server's close frame."""
        ws = RawWs(port, headers, timeout=20)
        try:
            ws.send(frame_bytes(TEXT, hello_bytes()))
            assert ws.recv() is not None  # the welcome: the session is live before the flood starts
            data = frame_bytes(PING, b"") * count
            sender = threading.Thread(target=self._send_all, args=(ws, data))
            sender.start()
            while (got := ws.recv()) is not None:
                if got[0] == 0x8:
                    self.close_code = int.from_bytes(got[1][:2], "big")
                    break
                self.pongs += got[0] == PONG
            sender.join(20)
        finally:
            ws.shut()

    @staticmethod
    def _send_all(ws: RawWs, data: bytes) -> None:
        with contextlib.suppress(OSError):  # closed by the server in the middle of the flood
            ws.send(data)


def test_twenty_thousand_pings_close_the_session_1008_and_never_hold_the_loop(tmp_path, monkeypatch):
    # the bucket never refills: pings arrive faster than a slow host answers them, and a real clock would refill
    monkeypatch.setattr(server_session, "ByteBudget", lambda: ByteBudget(clock=lambda: 0.0))
    result, w = PingFlood(), wiring(tmp_path)

    async def main() -> None:
        stop = asyncio.Event()
        async with serving(app_for(w)) as port:
            ticking = asyncio.create_task(result.probe(stop))
            await asyncio.get_running_loop().run_in_executor(None, result.client, port, w.headers(), FLOOD)
            stop.set()
            await ticking

    asyncio.run(asyncio.wait_for(main(), 60))
    assert result.close_code == 1008
    assert result.pongs <= 512  # 128 KiB burst / 256 B minimum charge, less what the hello cost
    assert result.max_gap < GAP_LIMIT, f"loop gap {result.max_gap * 1000:.0f} ms"
