# SPDX-License-Identifier: Apache-2.0
"""A stalled recompute: the adapter's waiter gives up after ``LOCK_TIMEOUT`` while the event loop keeps ticking.

The unit tests of the lock use a 0.05-0.1 s timeout and a recompute that finishes; this runs the shipped 2.0 s
default through ``connect()`` and ``handle_message()`` while another thread's recompute never returns.
"""

from __future__ import annotations

import asyncio
import threading
import time

from glasses import gate

from test_adapter import event, new_adapter
from test_gate import adapter_module  # noqa: F401 - fixture


def _stall(adapter) -> threading.Event:
    release, inside = threading.Event(), threading.Event()

    def compute() -> list[str]:
        inside.set()
        release.wait(30)
        return []

    adapter._gate_cache = gate.VerdictCache(compute=compute)  # the shipped LOCK_TIMEOUT
    holder = threading.Thread(target=adapter.gate_ok, daemon=True)
    holder.start()
    assert inside.wait(5)
    return release


def test_connect_is_refused_after_the_lock_timeout_and_the_loop_keeps_ticking_while_the_recompute_is_stalled(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
):
    adapter = new_adapter(adapter_module)
    release = _stall(adapter)

    async def main():
        ticks, stop = 0, asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        started = time.monotonic()
        admitted = await adapter.connect()
        waited = time.monotonic() - started
        stop.set()
        await task
        return admitted, waited, ticks

    try:
        admitted, waited, ticks = asyncio.run(main())
    finally:
        release.set()
    assert admitted is False
    assert gate.LOCK_TIMEOUT - 0.2 <= waited <= gate.LOCK_TIMEOUT + 1.5, waited
    assert ticks >= 5, ticks  # of an ideal 200 in the 2 s timeout; a loop blocked for it ticks 0 times


def test_an_inbound_event_is_dropped_after_the_lock_timeout_while_the_recompute_is_stalled(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
):
    adapter = new_adapter(adapter_module)
    release = _stall(adapter)
    inbound = event()
    try:
        started = time.monotonic()
        asyncio.run(adapter.handle_message(inbound))
        waited = time.monotonic() - started
    finally:
        release.set()
    assert inbound._gateway_accepted is False and adapter.handed_on == []
    assert gate.LOCK_TIMEOUT - 0.2 <= waited <= gate.LOCK_TIMEOUT + 1.5, waited
