# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), the inbound byte budget at its edges: bytes not characters, binary frames, the exact boundary of
a charge, a refused frame costing nothing, and a fresh budget for every connection."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from typing import Any

from server_helpers import app_for, serving, wiring
from server_raw_ws import BINARY, CLOSE, TEXT, RawWs, frame_bytes, say_hello, session

from glasses.connections import INBOUND_BURST_BYTES, INBOUND_MIN_CHARGE, ByteBudget

PING = b'{"v":1,"id":"01JABCDEFGHJKMNPQRSTVWXYZ0","type":"ping","ts":"2026-10-03T18:00:00Z","body":{}}'
INTS = b"[" + b",".join([b"1"] * 29_999) + b"]"  # 59 999 bytes: valid JSON, no envelope


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def burst(ws: RawWs, opcode: int, payload: bytes, count: int) -> tuple[int | None, int]:
    """Hello, then *count* identical frames back to back; ``(close code, replies read before it)``."""
    say_hello(ws)
    data = frame_bytes(opcode, payload) * count
    sender = threading.Thread(target=_send, args=(ws, data))
    sender.start()
    replies = 0
    code: int | None = None
    while (got := ws.recv()) is not None:
        if got[0] == CLOSE:
            code = int.from_bytes(got[1][:2], "big")
            break
        replies += 1
    sender.join(10)
    return code, replies


def _send(ws: RawWs, data: bytes) -> None:
    with contextlib.suppress(OSError):  # the server closed in the middle of the flood
        ws.send(data)


def test_a_text_frame_is_charged_its_utf8_bytes_not_its_characters(tmp_path):
    """20 000 euro signs are 60 002 bytes as a JSON string: two fit the 128 KiB burst after the hello, the third does
    not. Charged by characters (20 002 each) all four would pass."""
    payload = b'"' + "\u20ac".encode() * 20_000 + b'"'
    assert len(payload) == 60_002
    code, replies = session(wiring(tmp_path), lambda ws: burst(ws, TEXT, payload, 4))
    assert code == 1008 and replies <= 2  # at most two error frames (a string is no envelope): close drops the queue


def test_a_binary_frame_is_charged_like_a_text_frame(tmp_path):
    code, replies = session(wiring(tmp_path), lambda ws: burst(ws, BINARY, INTS, 4))
    assert code == 1008 and 1 <= replies <= 2


def test_the_budget_belongs_to_the_connection_a_new_one_of_the_same_device_starts_full(tmp_path):
    w = wiring(tmp_path)

    def work(port: int) -> tuple[int | None, dict[str, Any]]:
        first = RawWs(port, w.headers(), timeout=10)
        try:
            code, _ = burst(first, TEXT, INTS, 6)
        finally:
            first.shut()
        second = RawWs(port, w.headers(), timeout=10)
        try:
            say_hello(second)
            second.send(frame_bytes(TEXT, PING))
            got = second.recv()
            assert got is not None and got[0] == TEXT
            return code, json.loads(got[1])
        finally:
            second.shut()

    async def main() -> tuple[int | None, dict[str, Any]]:
        async with serving(app_for(w)) as port:
            return await asyncio.get_running_loop().run_in_executor(None, work, port)

    code, pong = asyncio.run(asyncio.wait_for(main(), 60))
    assert code == 1008 and pong["type"] == "pong"


def test_a_charge_equal_to_what_is_left_is_allowed_and_one_byte_more_is_not():
    budget = ByteBudget(clock=Clock())
    assert budget.allow(INBOUND_BURST_BYTES - 1_000)
    assert not budget.allow(1_001)
    assert budget.allow(1_000)  # a refused frame took nothing: exactly the 1 000 left are still there
    assert not budget.allow(INBOUND_MIN_CHARGE)


def test_a_refused_frame_costs_nothing_and_the_refill_is_counted_from_the_refusal():
    clock = Clock()
    budget = ByteBudget(clock=clock)
    assert budget.allow(INBOUND_BURST_BYTES) and not budget.allow(1)  # a new budget holds exactly the burst
    clock.now += 0.25
    assert not budget.allow(INBOUND_BURST_BYTES)  # 32 KiB back: refused, nothing taken
    clock.now += 0.25
    assert budget.allow(64 * 1024) and not budget.allow(256)  # 0.5 s in all: 64 KiB, not 32 KiB twice over
