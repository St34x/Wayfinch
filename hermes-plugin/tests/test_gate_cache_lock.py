# SPDX-License-Identifier: Apache-2.0
"""The allow-all gate's lock: a recompute that never returns must not freeze the gateway or admit anyone.

A read that blocks (a FIFO as ``.env``, a hung network home) holds the cache's lock. Every other caller waits at most
``LOCK_TIMEOUT`` seconds and is refused with a fixed text; nothing is stored for it and it does not recompute. The
adapter's ``connect()`` and ``handle_message()`` wait in a worker thread, so the event loop keeps running. Fakes only;
logging is in test_gate_refusal_log.py.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from glasses import gate

from test_adapter import ADAPTER_MODULE, event, new_adapter
from test_gate import adapter_module  # noqa: F401 - fixture
from test_gate_cache import Counting

REFUSED = [gate.LOCK_TIMEOUT_REFUSAL]


class Blocker:
    """A ``compute`` whose first call blocks until ``release`` is set; later calls return at once."""

    def __init__(self, problems: list[str] | None = None):
        self.inside, self.release = threading.Event(), threading.Event()
        self.problems = problems or []
        self.calls = 0

    def compute(self) -> list[str]:
        self.calls += 1
        if self.calls == 1:
            self.inside.set()
            assert self.release.wait(10), "the blocked recompute was never released"
        return list(self.problems)


def _start(work) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(work()))
    thread.start()
    return thread, out


def _finish(thread: threading.Thread) -> None:
    thread.join(10)
    assert not thread.is_alive()


def test_a_caller_waiting_for_a_blocked_recompute_is_refused_within_the_timeout_and_does_not_recompute():
    blocker, fake = Blocker(), Counting()
    cache = gate.VerdictCache(clock=fake.clock, compute=blocker.compute, lock_timeout=0.1)
    holder, held = _start(cache.violations)
    try:
        assert blocker.inside.wait(5)
        waiter, answer = _start(cache.violations)
        waiter.join(3)  # a lock wait without a timeout would still be waiting here, the blocker not released
        assert not waiter.is_alive(), "the second caller is still waiting for the lock"
        assert answer == [REFUSED]
        assert blocker.calls == 1
    finally:
        blocker.release.set()
    _finish(holder)
    assert held == [[]]  # the recompute that held the lock finished normally


def test_once_a_recompute_has_outlived_the_timeout_a_new_caller_is_refused_at_once_and_a_finished_one_is_not_stalled():
    blocker, fake = Blocker(), Counting()
    cache = gate.VerdictCache(clock=fake.clock, compute=blocker.compute, lock_timeout=5.0)
    holder, held = _start(cache.violations)
    try:
        assert blocker.inside.wait(5)
        fake.now += 5.5  # the recompute has run longer than the wait: nothing is gained by waiting for it
        started = time.monotonic()
        assert cache.violations() == REFUSED
        assert time.monotonic() - started < 1.0, "the caller waited for the lock"
    finally:
        blocker.release.set()
    _finish(holder)
    fake.now += 10
    assert cache.violations() == [] and blocker.calls == 2  # the next call after the stall is judged normally


def test_after_the_blocker_is_released_the_next_call_is_judged_normally_and_the_refusal_was_not_stored():
    blocker, fake = Blocker(), Counting()
    cache = gate.VerdictCache(clock=fake.clock, compute=blocker.compute, lock_timeout=0.05)
    holder, _ = _start(cache.violations)
    try:
        assert blocker.inside.wait(5)
        assert cache.violations() == REFUSED
    finally:
        blocker.release.set()
    _finish(holder)
    assert cache.violations() == []  # the holder's clean verdict, not the refusal
    assert blocker.calls == 1
    fake.now = 1.0  # the verdict expires: a normal recompute
    assert cache.violations() == [] and blocker.calls == 2


def test_the_holders_own_refusal_is_stored_not_the_timeout_refusal_of_a_waiter():
    blocker = Blocker(["profile dev: bad"])
    cache = gate.VerdictCache(clock=lambda: 0.0, compute=blocker.compute, lock_timeout=0.05)
    holder, held = _start(cache.violations)
    try:
        assert blocker.inside.wait(5)
        assert cache.violations() == REFUSED
    finally:
        blocker.release.set()
    _finish(holder)
    assert held == [["profile dev: bad"]] and cache.violations() == ["profile dev: bad"]


def test_many_waiters_are_all_refused_and_none_recomputes():
    blocker = Blocker()
    cache = gate.VerdictCache(clock=lambda: 0.0, compute=blocker.compute, lock_timeout=0.05)
    holder, _ = _start(cache.violations)
    try:
        assert blocker.inside.wait(5)
        waiters = [_start(cache.violations) for _ in range(16)]
        for thread, _out in waiters:
            _finish(thread)
        assert [out for _thread, out in waiters] == [[REFUSED]] * 16
        assert blocker.calls == 1
    finally:
        blocker.release.set()
    _finish(holder)


def test_the_default_timeout_is_the_module_constant(monkeypatch):
    monkeypatch.setattr(gate, "LOCK_TIMEOUT", 0.05)
    blocker = Blocker()
    cache = gate.VerdictCache(clock=lambda: 0.0, compute=blocker.compute)
    holder, _ = _start(cache.violations)
    try:
        assert blocker.inside.wait(5)
        waiter, answer = _start(cache.violations)
        waiter.join(3)
        assert not waiter.is_alive() and answer == [REFUSED]
    finally:
        blocker.release.set()
    _finish(holder)


def test_the_shipped_timeout_is_two_seconds_and_the_text_names_no_value():
    assert gate.LOCK_TIMEOUT == 2.0
    assert gate.LOCK_TIMEOUT_REFUSAL == "allow-all gate check timed out; refusing"


def test_an_error_in_the_holders_recompute_still_releases_the_lock_for_the_next_caller():
    fake = Counting()
    fake.error = ZeroDivisionError()
    cache = gate.VerdictCache(clock=fake.clock, compute=fake.compute, lock_timeout=0.05)
    with pytest.raises(ZeroDivisionError):
        cache.violations()
    fake.error = None
    assert cache.violations() == []  # not a timeout refusal


def test_an_error_out_of_the_recompute_leaves_no_running_mark_so_the_next_call_recomputes_later():
    fake = Counting()
    fake.error = ValueError("raised by the recompute")
    cache = gate.VerdictCache(clock=fake.clock, compute=fake.compute)
    with pytest.raises(ValueError):
        cache.violations()
    fake.error, fake.now = None, fake.now + 5  # longer than LOCK_TIMEOUT: a left-over mark would refuse at once
    assert cache.violations() == []  # recomputed, not refused as "timed out"
    assert fake.computed == 2
    assert cache._started is None


# -- the adapter ---------------------------------------------------------------------------------------------------


def test_the_adapter_refuses_a_caller_that_waits_for_a_blocked_recompute(adapter_module, caplog):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    blocker = Blocker()
    adapter._gate_cache = gate.VerdictCache(compute=blocker.compute, lock_timeout=0.05)
    holder, held = _start(adapter.gate_ok)
    try:
        assert blocker.inside.wait(5)
        with caplog.at_level("WARNING", logger=ADAPTER_MODULE):
            assert asyncio.run(adapter.connect()) is False
            inbound = event()
            asyncio.run(adapter.handle_message(inbound))
    finally:
        blocker.release.set()
    _finish(holder)
    assert held == [True]
    assert [code for code, _, _ in adapter.fatal_errors] == ["allow_all_gate"]
    assert adapter.handed_on == [] and inbound._gateway_accepted is False
    assert gate.LOCK_TIMEOUT_REFUSAL in caplog.text


@pytest.mark.parametrize("entry", ["connect", "handle_message"])
def test_the_event_loop_keeps_ticking_while_the_adapter_waits_for_a_slow_recompute(adapter_module, monkeypatch, entry):  # noqa: F811 - the imported fixture is used by name
    ticked = threading.Event()  # set by the ticker once the loop has run five times
    inner = gate.sources

    def waits_for_the_loop():  # runs in the adapter's worker thread; a blocked loop never sets the event
        ticked.wait(5)
        return inner()

    monkeypatch.setattr(gate, "sources", waits_for_the_loop)
    adapter = new_adapter(adapter_module)

    async def main():
        ticks, stop = 0, asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                if ticks == 5:
                    ticked.set()
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        if entry == "connect":
            await adapter.connect()
        else:
            await adapter.handle_message(event())
        stop.set()
        await task
        return ticks

    ticks = asyncio.run(main())
    assert ticks >= 5, ticks  # a loop blocked by a synchronous check would have ticked 0 times
