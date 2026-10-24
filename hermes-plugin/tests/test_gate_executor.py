# SPDX-License-Identifier: Apache-2.0
"""The gate checks run in the adapter's own small executor, never the loop's default one, and fail closed.

While a recompute is stalled (a ``.env`` that is a FIFO) a flood of ``connect()``/``handle_message()`` checks must
neither delay an unrelated ``asyncio.to_thread`` caller nor leave threads behind, and a gate check that cannot even be
submitted (executor shut down) refuses instead of raising into the gateway.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time

import pytest

from glasses import gate

from test_adapter import ADAPTER_MODULE, event, new_adapter
from test_gate import adapter_module  # noqa: F401 - fixture
from test_gate_stalled_recompute_adapter import _stall

FLOOD = 30
GATE_WORKERS = 2


def _wait_for_threads(baseline: int) -> int:
    deadline = time.monotonic() + 5
    while threading.active_count() > baseline and time.monotonic() < deadline:
        time.sleep(0.05)
    return threading.active_count()


def test_a_flood_of_checks_during_a_stalled_recompute_leaves_the_default_executor_free_and_all_are_refused(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
    caplog,
):
    baseline = threading.active_count()
    adapter = new_adapter(adapter_module)
    release = _stall(adapter)
    timings: dict[str, float] = {}

    async def main() -> list[bool]:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        started = time.monotonic()
        flood = [asyncio.create_task(adapter.gate_ok_async()) for _ in range(FLOOD)]
        await asyncio.sleep(0.2)  # every check is submitted and waiting for the stalled lock
        probe = time.monotonic()
        await asyncio.to_thread(time.monotonic)
        timings["sibling"] = time.monotonic() - probe
        answers = await asyncio.gather(*flood)
        timings["flood"] = time.monotonic() - started
        return list(answers)

    try:
        with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
            answers = asyncio.run(main())
    finally:
        release.set()
    asyncio.run(adapter.disconnect())
    assert answers == [False] * FLOOD
    assert gate.LOCK_TIMEOUT_REFUSAL in caplog.text
    assert timings["sibling"] < 0.5, timings  # the default executor was not occupied by the waiting checks
    assert timings["flood"] < gate.LOCK_TIMEOUT + 1.5, timings  # not N/workers x LOCK_TIMEOUT
    assert _wait_for_threads(baseline) <= baseline


def _dead_executor() -> concurrent.futures.ThreadPoolExecutor:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pool.shutdown()  # a later submit raises RuntimeError
    return pool


def test_connect_is_refused_when_the_check_cannot_be_submitted(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    monkeypatch.setattr(adapter, "_gate_pool", _dead_executor)
    with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
        assert asyncio.run(adapter.connect()) is False
    assert "allow-all gate check failed (RuntimeError)" in caplog.text
    assert adapter.fatal_errors == [("allow_all_gate", "glasses allow-all gate open (see log)", True)]


def test_an_inbound_event_is_dropped_when_the_check_cannot_be_submitted(adapter_module, monkeypatch):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    monkeypatch.setattr(adapter, "_gate_pool", _dead_executor)
    inbound = event()
    asyncio.run(adapter.handle_message(inbound))
    assert inbound._gateway_accepted is False and adapter.handed_on == []


def _gate_threads(before: set[threading.Thread], limit: int = 0) -> int:
    """Number of ``glasses-gate`` threads started since ``before`` once there are at most ``limit`` (or after 5 s)."""
    deadline = time.monotonic() + 5
    while True:
        count = sum(1 for t in threading.enumerate() if t.name.startswith("glasses-gate") and t not in before)
        if count <= limit or time.monotonic() > deadline:
            return count
        time.sleep(0.05)


def test_the_adapter_is_judged_normally_after_a_disconnect_and_a_reconnect(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
    caplog,
):
    before = set(threading.enumerate())  # other tests may have left idle workers of their own pools
    adapter = new_adapter(adapter_module)
    with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
        assert asyncio.run(adapter.connect()) is False
        first_pool = adapter._pool
        asyncio.run(adapter.disconnect())
        with pytest.raises(RuntimeError):
            first_pool.submit(time.monotonic)  # the old pool was stopped, not kept
        assert asyncio.run(adapter.gate_ok_async()) is True  # a new pool answers
        assert asyncio.run(adapter.connect()) is False
        for _ in range(5):
            asyncio.run(adapter.connect())
            asyncio.run(adapter.disconnect())
    assert [code for code, _text, _retryable in adapter.fatal_errors] == ["edge_listener"] * 7
    assert "check failed" not in caplog.text
    assert _gate_threads(before, GATE_WORKERS) <= GATE_WORKERS  # the workers of stopped pools end; they do not pile up
    asyncio.run(adapter.disconnect())
    assert _gate_threads(before) == 0
