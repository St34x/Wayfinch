# SPDX-License-Identifier: Apache-2.0
"""The adapter's async allow-all check runs in a gate worker thread and leaves the event loop free.

The tick-counting tests (test_gate_cache_lock.py and its siblings) show this by waiting for loop ticks; these two cases
state the same property without a clock: the check runs on a thread named ``glasses-gate*`` that is not the loop's
thread, and a coroutine the check's own thread schedules on the loop completes while the check is still running.
Fakes only.
"""

from __future__ import annotations

import asyncio
import threading

from glasses import gate

from test_adapter import new_adapter
from test_gate import adapter_module  # noqa: F401 - fixture


def test_the_async_gate_check_runs_in_a_gate_worker_thread_not_on_the_loop_thread(adapter_module, monkeypatch):  # noqa: F811 - the imported fixture is used by name
    inner = gate.sources
    seen: list[tuple[int, str]] = []

    def probe():
        seen.append((threading.get_ident(), threading.current_thread().name))
        return inner()

    monkeypatch.setattr(gate, "sources", probe)
    adapter = new_adapter(adapter_module)

    async def main() -> tuple[bool, int]:
        return await adapter.gate_ok_async(), threading.get_ident()

    admitted, loop_thread = asyncio.run(main())
    assert admitted is True
    assert len(seen) == 1
    assert seen[0][0] != loop_thread
    assert seen[0][1].startswith("glasses-gate"), seen


def test_the_loop_runs_a_coroutine_while_the_gate_check_is_still_running(adapter_module, monkeypatch):  # noqa: F811 - the imported fixture is used by name
    inner = gate.sources
    outcome: list[str] = []
    loop_holder: list[asyncio.AbstractEventLoop] = []

    async def on_the_loop() -> str:
        return "ran on the loop while the check was running"

    def check_that_needs_the_loop():  # a check on the loop thread would wait here for the loop it is blocking
        future = asyncio.run_coroutine_threadsafe(on_the_loop(), loop_holder[0])
        try:
            outcome.append(future.result(5))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome.append(type(exc).__name__)
        return inner()

    monkeypatch.setattr(gate, "sources", check_that_needs_the_loop)
    adapter = new_adapter(adapter_module)

    async def main() -> bool:
        loop_holder.append(asyncio.get_running_loop())
        return await adapter.gate_ok_async()

    assert asyncio.run(main()) is True
    assert outcome == ["ran on the loop while the check was running"]
