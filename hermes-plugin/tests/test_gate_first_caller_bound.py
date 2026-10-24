# SPDX-License-Identifier: Apache-2.0
"""The first caller on a stalled recompute is bounded too: it runs the blocked read itself and is refused after
``LOCK_TIMEOUT + 1`` seconds, while the event loop keeps ticking.

Real clock with the shipped constants: each of the three entry-point cases takes about 3 s (the whole file about 10 s).
No other thread holds the lock here, so the awaiting check is the one that drives the stalled ``compute``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from glasses import gate

from test_adapter import ADAPTER_MODULE, event, new_adapter
from test_gate import adapter_module  # noqa: F401 - fixture

BOUND = gate.LOCK_TIMEOUT + 1.5  # the adapter gives up after LOCK_TIMEOUT + 1; the rest is scheduling slack
MIN_TICKS = 5  # of an ideal 300 in the ~3 s wait: shows the loop ran, whatever the host load


def _block(adapter) -> threading.Event:
    """Make ``compute`` block until the returned event is set (a 30 s cap ends it even when a test fails)."""
    release = threading.Event()

    def compute() -> list[str]:
        release.wait(30)
        return []

    adapter._gate_cache = gate.VerdictCache(compute=compute)  # the shipped LOCK_TIMEOUT
    return release


async def _timed_with_ticker(call):
    """Await ``call()`` while a 10 ms ticker runs; returns (result, seconds waited, ticks)."""
    ticks, stop = 0, asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    result = await call()
    waited = time.monotonic() - started
    stop.set()
    await task
    return result, waited, ticks


def _gate_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("glasses-gate")]


def _wait_for_no_new_gate_threads(before: list[threading.Thread]) -> list[threading.Thread]:
    deadline = time.monotonic() + 5
    while set(_gate_threads()) - set(before) and time.monotonic() < deadline:
        time.sleep(0.05)
    return [t for t in _gate_threads() if t not in before]


def test_the_check_that_runs_the_stalled_recompute_is_refused_after_about_three_seconds_and_the_loop_keeps_ticking(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
    caplog,
):
    before = _gate_threads()  # other tests may have left idle workers of their own pools
    adapter = new_adapter(adapter_module)
    release = _block(adapter)

    async def main():
        first = await _timed_with_ticker(adapter.gate_ok_async)
        started = time.monotonic()
        second = await adapter.gate_ok_async()  # the read is still blocked: the fast path
        return first, second, time.monotonic() - started

    try:
        with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
            (admitted, waited, ticks), second, second_waited = asyncio.run(main())
        assert admitted is False
        assert gate.LOCK_TIMEOUT - 0.2 <= waited <= BOUND, waited
        assert ticks >= MIN_TICKS, ticks  # a loop blocked for the whole wait would have ticked 0 times
        refusals = [r.getMessage() for r in caplog.records if r.name == ADAPTER_MODULE and r.levelno == logging.WARNING]
        assert refusals[0] == "glasses adapter: refusing: allow-all gate check failed (TimeoutError)"
        assert second is False and second_waited < 0.5, second_waited
    finally:
        release.set()
    time.sleep(0.2)  # the released worker ends its read
    assert asyncio.run(adapter.gate_ok_async()) is True  # judged normally again
    asyncio.run(adapter.disconnect())
    assert _wait_for_no_new_gate_threads(before) == []


def test_the_first_connect_on_a_stalled_recompute_is_refused_after_about_three_seconds(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
):
    adapter = new_adapter(adapter_module)
    release = _block(adapter)
    try:
        admitted, waited, ticks = asyncio.run(_timed_with_ticker(adapter.connect))
    finally:
        release.set()
    asyncio.run(adapter.disconnect())
    assert admitted is False
    assert gate.LOCK_TIMEOUT - 0.2 <= waited <= BOUND, waited
    assert ticks >= MIN_TICKS, ticks
    assert [name for name, _text, _retryable in adapter.fatal_errors] == ["allow_all_gate"]


def test_the_first_inbound_event_on_a_stalled_recompute_is_dropped_after_about_three_seconds(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
):
    adapter = new_adapter(adapter_module)
    release = _block(adapter)
    inbound = event()
    try:
        _none, waited, ticks = asyncio.run(_timed_with_ticker(lambda: adapter.handle_message(inbound)))
    finally:
        release.set()
    asyncio.run(adapter.disconnect())
    assert inbound._gateway_accepted is False and adapter.handed_on == []
    assert gate.LOCK_TIMEOUT - 0.2 <= waited <= BOUND, waited
    assert ticks >= MIN_TICKS, ticks
