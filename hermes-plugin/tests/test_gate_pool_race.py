# SPDX-License-Identifier: Apache-2.0
"""The adapter builds its gate executor once, under a lock, however many threads (each with its own loop) ask first.

The gateway calls one adapter from one loop's thread, so this cannot race there; the lock keeps it true if an adapter is
ever called from several loops or threads, instead of leaking the idle ``glasses-gate`` workers of the lost pools.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from types import SimpleNamespace

from glasses import gate

from test_adapter import new_adapter
from test_gate import adapter_module  # noqa: F401 - fixture

ROUNDS = 30
CALLERS = 8
GATE_WORKERS = 2


def _gate_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name.startswith("glasses-gate")}


def _slow_pool_class(built: list[int]) -> type:
    class SlowPool(concurrent.futures.ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            built.append(1)
            time.sleep(0.005)  # widens the window between "no pool yet" and "pool stored"
            super().__init__(*args, **kwargs)

    return SlowPool


def _all_callers_ask_at_once(adapter) -> list[bool]:
    start, answers = threading.Barrier(CALLERS), [False] * CALLERS

    def run(index: int) -> None:
        start.wait(5)
        answers[index] = asyncio.run(adapter.gate_ok_async())

    threads = [threading.Thread(target=run, args=(i,)) for i in range(CALLERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    return answers


def test_callers_in_several_threads_with_their_own_loops_create_exactly_one_pool(adapter_module, monkeypatch):  # noqa: F811 - the imported fixture is used by name
    built: list[int] = []
    shim = SimpleNamespace(futures=SimpleNamespace(ThreadPoolExecutor=_slow_pool_class(built)))
    monkeypatch.setattr(adapter_module, "concurrent", shim)  # only the adapter's pool; asyncio keeps the real class
    for _ in range(ROUNDS):
        before = _gate_threads()  # other tests may have left idle workers of their own pools
        adapter = new_adapter(adapter_module)
        adapter._gate_cache = gate.VerdictCache(compute=list)
        built.clear()
        answers = _all_callers_ask_at_once(adapter)
        threads = len(_gate_threads() - before)
        asyncio.run(adapter.disconnect())
        assert answers == [True] * CALLERS
        assert len(built) == 1, len(built)
        assert threads <= GATE_WORKERS, threads


def test_a_check_in_flight_when_the_adapter_disconnects_still_completes(adapter_module):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    inside, release = threading.Event(), threading.Event()

    def compute() -> list[str]:
        inside.set()
        release.wait(10)
        return []

    adapter._gate_cache = gate.VerdictCache(compute=compute)

    async def main() -> bool:
        check = asyncio.create_task(adapter.gate_ok_async())
        deadline = time.monotonic() + 5
        while not inside.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert inside.is_set()
        await adapter.disconnect()  # stops the pool the check is running in
        release.set()
        return await check

    try:
        assert asyncio.run(main()) is True
    finally:
        release.set()
