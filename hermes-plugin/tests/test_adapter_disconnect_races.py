# SPDX-License-Identifier: Apache-2.0
"""Edge cases of ``GlassesAdapter.disconnect()`` waiting for ``connect()``, against a REAL ``EdgeListener`` on a
real unix socket: after every scenario that ends with ``disconnect()`` the socket file is gone and nothing accepts.
Also pins two accepted gaps as they are (a ``connect()`` still in the gate before the lock, a cancelled
``disconnect()`` while a start is in flight): a listener may outlive that ``disconnect()``, a second one stops it."""

from __future__ import annotations

import asyncio
import os
import random
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from edge_helpers import short_dir

from glasses.edge_listener import EdgeListener

from test_adapter import EDGE_SOCKET, adapter_module, new_adapter  # noqa: F401 - the fixture is used by name

EXTRA = {"edge": {"socket": EDGE_SOCKET, "user": "edge-proxy"}}


@pytest.fixture
def sock_path() -> Iterator[Path]:
    with short_dir() as directory:
        yield directory / "e.sock"


def _accepts(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError:
            return False
        return True


def _gone(path: Path) -> bool:
    return not path.exists() and not _accepts(path)


class _Noop:
    async def cleanup(self) -> None:
        return None

    def close(self) -> None:
        return None


class _NoopOutbox:
    async def close(self) -> None:
        return None


def rig(module, monkeypatch, path: Path, *, delay: float = 0.1, fail: bool = False, stop_delay: float = 0.0):
    """Patch ``_start_edge`` to start a real ``EdgeListener`` at *path* after *delay* seconds (or raise instead)."""
    log: dict[str, list[object]] = {"started": [], "stopped": []}

    async def start(_cfg, _gate):
        await asyncio.sleep(delay)
        if fail:
            raise OSError("start fails")
        listener = EdgeListener(str(path), os.geteuid())
        await listener.start(asyncio.Protocol)
        log["started"].append(listener)
        edge = module._Edge(listener, _Noop(), _Noop(), _NoopOutbox(), _Noop(), _Noop())

        real_stop = edge.stop

        async def stop() -> None:
            log["stopped"].append(listener)
            if stop_delay:
                await asyncio.sleep(stop_delay)
            await real_stop()

        edge.stop = stop  # type: ignore[method-assign]
        return edge

    monkeypatch.setattr(module, "_start_edge", start)
    adapter = new_adapter(module, EXTRA)

    async def gate_open() -> bool:
        return True

    adapter.gate_ok_async = gate_open  # type: ignore[method-assign]
    return adapter, log


def test_disconnect_in_the_middle_of_a_successful_start_leaves_no_listener(adapter_module, monkeypatch, sock_path):  # noqa: F811 - the imported fixture is used by name
    adapter, log = rig(adapter_module, monkeypatch, sock_path)

    async def run() -> bool:
        starting = asyncio.ensure_future(adapter.connect())
        await asyncio.sleep(0.03)
        await adapter.disconnect()
        return await starting

    assert asyncio.run(run()) is True
    assert len(log["started"]) == 1 and log["stopped"] == log["started"]
    assert adapter._edge is None and adapter.disconnected is True and _gone(sock_path)


def test_disconnect_in_the_middle_of_a_failing_start_leaves_nothing(adapter_module, monkeypatch, sock_path):  # noqa: F811 - the imported fixture is used by name
    adapter, log = rig(adapter_module, monkeypatch, sock_path, fail=True)

    async def run() -> bool:
        starting = asyncio.ensure_future(adapter.connect())
        await asyncio.sleep(0.03)
        await asyncio.wait_for(adapter.disconnect(), 5)
        return await starting

    assert asyncio.run(run()) is False
    assert log["started"] == [] and adapter._edge is None and adapter.disconnected is True and _gone(sock_path)


def test_connect_racing_disconnect_never_leaves_an_orphan_listener(adapter_module, monkeypatch, sock_path):  # noqa: F811 - the imported fixture is used by name
    """200 rounds, connect() and disconnect() offset by 0..8 ms (fixed seed), through the gate (before the lock)
    and the start (under it). A listener may be up after a round only if the adapter holds it (disconnect() ran
    first, connect() then started: a legal order); never an orphan, never two; the final disconnect() always clears."""
    rng = random.Random(184)  # noqa: S311 - a deterministic test schedule
    adapter, log = rig(adapter_module, monkeypatch, sock_path, delay=0.001)
    gate_delay = {"v": 0.0}

    async def slow_gate() -> bool:
        await asyncio.sleep(gate_delay["v"])
        return True

    adapter.gate_ok_async = slow_gate  # type: ignore[method-assign]
    outcomes = {"stopped": 0, "relistening": 0}

    async def run() -> None:
        for _ in range(200):
            gate_delay["v"] = rng.uniform(0, 0.004)
            before = len(log["started"])
            connecting = asyncio.ensure_future(adapter.connect())
            await asyncio.sleep(rng.uniform(0, 0.008))
            await asyncio.wait_for(adapter.disconnect(), 5)
            assert await asyncio.wait_for(connecting, 5) is True
            assert len(log["started"]) - before == 1  # exactly one listener per round, never two
            held = adapter._edge is not None
            assert held == _accepts(sock_path)  # bound exactly when the adapter holds it: no orphan
            outcomes["relistening" if held else "stopped"] += 1
            await adapter.disconnect()
            assert _gone(sock_path) and adapter._edge is None

    asyncio.run(run())
    assert sum(outcomes.values()) == 200 and outcomes["stopped"] > 0 and outcomes["relistening"] > 0, outcomes
    assert len(log["stopped"]) == len(log["started"])  # every listener started was stopped


def test_cancelling_a_disconnect_that_waits_keeps_the_start_and_a_second_disconnect_clears_it(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
    monkeypatch,
    sock_path,
):
    adapter, log = rig(adapter_module, monkeypatch, sock_path, delay=0.3)

    async def run() -> bool:
        starting = asyncio.ensure_future(adapter.connect())
        await asyncio.sleep(0.05)
        waiting = asyncio.ensure_future(adapter.disconnect())
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert adapter.disconnected is True  # the finally of the cancelled disconnect() ran
        assert await starting is True
        assert _accepts(sock_path) and adapter._edge is not None  # accepted gap: a listener outlives that disconnect()
        await asyncio.wait_for(adapter.disconnect(), 5)  # the lock was released by the cancel
        return True

    assert asyncio.run(run()) is True
    assert adapter._edge is None and _gone(sock_path) and log["stopped"] == log["started"]


def test_cancelling_a_disconnect_that_is_stopping_releases_the_lock(adapter_module, monkeypatch, sock_path):  # noqa: F811 - the imported fixture is used by name
    adapter, log = rig(adapter_module, monkeypatch, sock_path, delay=0.0, stop_delay=3600)

    async def run() -> None:
        assert await adapter.connect() is True
        stopping = asyncio.ensure_future(adapter.disconnect())
        await asyncio.sleep(0.05)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert adapter._edge is None and adapter.disconnected is True
        # not wedged: connect() returns at once; the listener the hung stop left bound makes the new start refuse
        assert await asyncio.wait_for(adapter.connect(), 5) is False

    asyncio.run(run())


def test_double_disconnect_is_harmless_idle_and_concurrent(adapter_module, monkeypatch, sock_path):  # noqa: F811 - the imported fixture is used by name
    adapter, log = rig(adapter_module, monkeypatch, sock_path, delay=0.0, stop_delay=0.05)

    async def run() -> None:
        await adapter.disconnect()  # nothing started
        assert await adapter.connect() is True
        await asyncio.wait_for(asyncio.gather(adapter.disconnect(), adapter.disconnect()), 5)
        assert _gone(sock_path) and adapter._edge is None
        await adapter.disconnect()
        assert await adapter.connect() is True  # and a connect afterwards starts afresh
        await adapter.disconnect()

    asyncio.run(run())
    assert len(log["started"]) == 2 and log["stopped"] == log["started"] and _gone(sock_path)


def test_connect_still_in_the_gate_starts_a_listener_after_disconnect(adapter_module, monkeypatch, sock_path):  # noqa: F811 - the imported fixture is used by name
    """Known gap: a connect() awaiting the gate (before the lock) when disconnect() runs starts afterwards.
    Pinned as today's behaviour; a generation counter would flip the two asserts."""
    adapter, log = rig(adapter_module, monkeypatch, sock_path, delay=0.0)

    async def run() -> None:
        gate = asyncio.Event()

        async def held_gate() -> bool:
            await gate.wait()
            return True

        adapter.gate_ok_async = held_gate  # type: ignore[method-assign]
        connecting = asyncio.ensure_future(adapter.connect())
        await asyncio.sleep(0.03)
        await adapter.disconnect()  # nothing to wait for: the lock is free
        gate.set()
        assert await connecting is True
        assert adapter.disconnected is True and adapter._edge is not None and _accepts(sock_path)
        await adapter.disconnect()

    asyncio.run(run())
    assert _gone(sock_path)
