# SPDX-License-Identifier: Apache-2.0
"""``GlassesAdapter.connect()`` / ``disconnect()`` around the edge start: two overlapping ``connect()``
calls start one listener; an unexpected error is the retryable fatal error with the type name only; a failing
``_Edge.stop`` still cleans up the runner, last-seen and the gate's worker pool."""

from __future__ import annotations

import asyncio
import types

import pytest

from test_adapter import EDGE_SOCKET, adapter_module, new_adapter  # noqa: F401 - the fixture is used by name

EXTRA = {"edge": {"socket": EDGE_SOCKET, "user": "edge-proxy"}}


class _Recorder:
    """Stands in for the listener, runner and last-seen writer of an ``_Edge``; ``stop`` of the listener may raise."""

    def __init__(self, fail: bool) -> None:
        self.fail, self.calls = fail, []

    async def stop(self) -> None:
        self.calls.append("listener")
        if self.fail:
            raise RuntimeError("stop failed")

    async def cleanup(self) -> None:
        self.calls.append("runner")

    def close(self) -> None:
        self.calls.append("last_seen")


class _OutboxRecorder:
    """Stands in for the outbox of an ``_Edge`` (closed between the runner and last-seen)."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def close(self) -> None:
        self.calls.append("outbox")


def test_overlapping_connects_start_one_listener(adapter_module, monkeypatch):  # noqa: F811 - the imported fixture
    started: list[int] = []

    async def slow_start(_cfg, _gate):
        started.append(1)
        await asyncio.sleep(0.05)  # the second connect() arrives while the first is still starting
        return types.SimpleNamespace(stop=_Recorder(False).stop)

    monkeypatch.setattr(adapter_module, "_start_edge", slow_start)
    adapter = new_adapter(adapter_module, EXTRA)

    async def run() -> list[bool]:
        return list(await asyncio.gather(adapter.connect(), adapter.connect()))

    assert asyncio.run(run()) == [True, True]
    assert len(started) == 1 and adapter.fatal_errors == []


def test_an_unexpected_start_error_is_the_retryable_fatal_error_with_the_type_only(
    adapter_module,  # noqa: F811 - the imported fixture
    monkeypatch,
    caplog,
):
    async def broken(_cfg, _gate):
        raise RuntimeError("secret-sentinel")

    monkeypatch.setattr(adapter_module, "_start_edge", broken)
    adapter = new_adapter(adapter_module, EXTRA)
    assert asyncio.run(adapter.connect()) is False
    ((code, message, retryable),) = adapter.fatal_errors
    assert (code, retryable) == ("edge_listener", True) and "RuntimeError" in message
    assert "secret-sentinel" not in message + caplog.text


def test_a_failing_listener_stop_still_cleans_up_runner_and_last_seen(adapter_module):  # noqa: F811 - imported fixture
    recorder = _Recorder(True)
    check = types.SimpleNamespace(close=lambda: recorder.calls.append("revoked"))  # the frame check's poll
    edge = adapter_module._Edge(recorder, recorder, recorder, _OutboxRecorder(recorder.calls), recorder, check)
    with pytest.raises(RuntimeError, match="stop failed"):
        asyncio.run(edge.stop())
    assert recorder.calls == ["revoked", "listener", "runner", "outbox", "last_seen"]


def test_a_failing_edge_stop_still_shuts_gate_pool_and_marks_disconnected(adapter_module):  # noqa: F811 - imported fixture
    adapter = new_adapter(adapter_module, EXTRA)
    adapter._edge = types.SimpleNamespace(stop=_Recorder(True).stop)
    pool = adapter._gate_pool()

    with pytest.raises(RuntimeError, match="stop failed"):
        asyncio.run(adapter.disconnect())
    assert adapter._pool is None and adapter.disconnected is True
    with pytest.raises(RuntimeError, match="shutdown"):
        pool.submit(int)
