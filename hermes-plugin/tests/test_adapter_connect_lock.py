# SPDX-License-Identifier: Apache-2.0
"""The adapter's start lock: a failed or cancelled first ``connect()`` does not wedge the next one, sequential
``connect()`` after ``disconnect()`` starts afresh, ``disconnect()`` waits for a start that is still in flight (and
returns after one that fails while it waits), and a start failure of any exception type (not only the typed ones)
is the retryable fatal error and leaves ``connect()`` returning False."""

from __future__ import annotations

import asyncio
import types

import pytest

from test_adapter import EDGE_SOCKET, adapter_module, new_adapter  # noqa: F401 - the fixture is used by name

EXTRA = {"edge": {"socket": EDGE_SOCKET, "user": "edge-proxy"}}


def _edge() -> types.SimpleNamespace:
    async def stop() -> None:
        return None

    return types.SimpleNamespace(stop=stop)


def test_a_failed_first_connect_does_not_block_a_retry(adapter_module, monkeypatch):  # noqa: F811 - imported fixture
    attempts: list[int] = []

    async def flaky(_cfg, _gate):
        attempts.append(1)
        await asyncio.sleep(0.02)
        if len(attempts) == 1:
            raise OSError("first start fails")
        return _edge()

    monkeypatch.setattr(adapter_module, "_start_edge", flaky)
    adapter = new_adapter(adapter_module, EXTRA)

    async def run() -> list[bool]:
        first, second = await asyncio.gather(adapter.connect(), adapter.connect())
        third = await asyncio.wait_for(adapter.connect(), 5)
        return [first, second, third]

    results = asyncio.run(run())
    assert results[0] is False and results[1] is True and results[2] is True  # the waiter retried and won
    assert len(attempts) == 2


def test_a_cancelled_first_connect_releases_the_lock(adapter_module, monkeypatch):  # noqa: F811 - imported fixture
    async def hangs_once(_cfg, _gate):
        if not getattr(hangs_once, "seen", False):
            hangs_once.seen = True  # type: ignore[attr-defined]
            await asyncio.sleep(3600)
        return _edge()

    monkeypatch.setattr(adapter_module, "_start_edge", hangs_once)
    adapter = new_adapter(adapter_module, EXTRA)

    async def run() -> bool:
        first = asyncio.ensure_future(adapter.connect())
        await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await asyncio.wait_for(adapter.connect(), 5)

    assert asyncio.run(run()) is True


@pytest.mark.parametrize("exc", [RuntimeError("secret-sentinel"), ImportError("secret-sentinel"), KeyError("k")])
def test_any_start_exception_is_the_retryable_fatal_error(adapter_module, monkeypatch, caplog, exc):  # noqa: F811 - fixture
    async def broken(_cfg, _gate):
        raise exc

    monkeypatch.setattr(adapter_module, "_start_edge", broken)
    adapter = new_adapter(adapter_module, EXTRA)
    assert asyncio.run(adapter.connect()) is False
    ((code, message, retryable),) = adapter.fatal_errors
    assert (code, retryable) == ("edge_listener", True) and type(exc).__name__ in message
    assert "secret-sentinel" not in message + caplog.text


def test_connect_after_disconnect_starts_a_fresh_listener(adapter_module, monkeypatch):  # noqa: F811 - imported fixture
    started: list[int] = []

    async def start(_cfg, _gate):
        started.append(1)
        return _edge()

    monkeypatch.setattr(adapter_module, "_start_edge", start)
    adapter = new_adapter(adapter_module, EXTRA)

    async def run() -> None:
        assert await adapter.connect() and await adapter.connect()
        await adapter.disconnect()
        assert await adapter.connect()

    asyncio.run(run())
    assert len(started) == 2


def test_disconnect_waits_for_a_connect_that_is_still_starting(adapter_module, monkeypatch):  # noqa: F811 - fixture
    stops: list[int] = []

    async def stop() -> None:
        stops.append(1)

    async def slow_start(_cfg, _gate):
        await asyncio.sleep(0.2)
        return types.SimpleNamespace(stop=stop)

    monkeypatch.setattr(adapter_module, "_start_edge", slow_start)
    adapter = new_adapter(adapter_module, EXTRA)

    async def run() -> bool:
        starting = asyncio.ensure_future(adapter.connect())
        await asyncio.sleep(0.05)
        await adapter.disconnect()
        assert starting.done()  # disconnect() returned only after the start had finished
        return await starting

    assert asyncio.run(run()) is True
    assert adapter._edge is None and stops == [1]  # the edge the start produced was stopped, none is kept


def test_disconnect_returns_after_a_connect_that_fails_while_it_waits(adapter_module, monkeypatch):  # noqa: F811 - fixture
    async def failing_start(_cfg, _gate):
        await asyncio.sleep(0.1)
        raise OSError("start fails")

    monkeypatch.setattr(adapter_module, "_start_edge", failing_start)
    adapter = new_adapter(adapter_module, EXTRA)

    async def run() -> bool:
        starting = asyncio.ensure_future(adapter.connect())
        await asyncio.sleep(0.03)
        await asyncio.wait_for(adapter.disconnect(), 5)
        return await starting

    assert asyncio.run(run()) is False
    assert adapter._edge is None
