# SPDX-License-Identifier: Apache-2.0
"""Edges of the allow-all gate's verdict cache (``glasses.gate.VerdictCache``): concurrent callers, errors, a changed
served set, and the event loop. Fakes only; the real-Hermes half is test_gate_served_profiles_real.py, the one-second
rule is test_gate_cache.py.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from glasses import gate

from test_adapter import new_adapter
from test_gate import OPEN, SENTINEL, Hermes, adapter_module, counting_sources, install  # noqa: F401 - fixtures
from test_gate_cache import Counting

# -- concurrent callers --------------------------------------------------------------------------------------------


def _in_threads(count: int, work) -> list[Any]:
    start, results = threading.Barrier(count), [None] * count

    def run(index: int) -> None:
        start.wait(5)
        results[index] = work()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    return results


def test_concurrent_callers_share_one_recompute():
    fake = Counting()
    fake.problems = ["profile dev: bad"]
    slow = fake.compute
    fake.compute = lambda: (time.sleep(0.05), slow())[1]
    cache = fake.cache()
    results = _in_threads(16, cache.violations)
    assert results == [["profile dev: bad"]] * 16
    assert fake.computed == 1


def test_a_recompute_never_overlaps_another():
    active, widest = [0], [0]
    guard = threading.Lock()

    def compute():
        with guard:
            active[0] += 1
            widest[0] = max(widest[0], active[0])
        time.sleep(0.005)
        with guard:
            active[0] -= 1
        return []

    cache = gate.VerdictCache(ttl=0.0, clock=lambda: 0.0, compute=compute)
    _in_threads(12, lambda: [cache.violations() for _ in range(5)])
    assert widest[0] == 1


def test_after_the_window_every_thread_sees_the_new_verdict():
    fake = Counting()
    cache = fake.cache()
    assert cache.violations() == []
    fake.problems, fake.now = ["profile dev: bad"], 1.0
    assert _in_threads(12, cache.violations) == [["profile dev: bad"]] * 12
    assert fake.computed == 2


# -- errors --------------------------------------------------------------------------------------------------------


def test_an_error_in_the_recompute_releases_the_lock():
    fake = Counting()
    fake.error = ZeroDivisionError()
    cache = fake.cache()
    with pytest.raises(ZeroDivisionError):
        cache.violations()
    fake.error = None
    assert _in_threads(1, cache.violations) == [[]]  # a stuck lock would time the join out


def test_a_list_returned_from_a_cache_hit_cannot_change_the_cached_verdict():
    fake = Counting()
    fake.problems = ["profile dev: bad"]
    cache = fake.cache()
    cache.violations()
    cache.violations().append("injected")  # the second call is the first one served from the cache
    assert cache.violations() == ["profile dev: bad"]
    assert fake.computed == 1


def test_an_unreadable_scope_is_a_refusal_that_ends_a_second_after_the_scope_is_readable(monkeypatch):
    state = {"readable": False}

    def sources():
        if not state["readable"]:
            raise OSError(SENTINEL)
        return []

    monkeypatch.setattr(gate, "sources", sources)
    fake = Counting()
    cache = gate.VerdictCache(clock=fake.clock)
    (problem,) = cache.violations()
    assert "OSError" in problem and SENTINEL not in problem
    state["readable"], fake.now = True, 0.5
    assert cache.violations() == [problem]  # the refusal is cached like any verdict, for the window only
    fake.now = 1.0
    assert cache.violations() == []


def test_a_value_in_a_scope_error_never_reaches_the_adapter_log(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    def boom():
        raise ValueError(SENTINEL)

    monkeypatch.setattr(gate, "sources", boom)
    adapter = new_adapter(adapter_module)
    assert adapter.gate_ok() is False
    assert SENTINEL not in caplog.text and "ValueError" in caplog.text


# -- the served set ------------------------------------------------------------------------------------------------


def test_a_served_profile_swapped_for_another_without_any_file_is_seen_after_a_second(install):  # noqa: F811 - the imported fixture is used by name
    # The case the old per-profile file stamp could not tell apart: ``(None,)`` before and after the swap.
    hermes = install(Hermes({"one": {}}, parked={"two": OPEN}))
    fake = Counting()
    cache = gate.VerdictCache(clock=fake.clock)
    assert cache.violations() == []
    hermes.served = ["two"]  # "two" is un-parked and "one" leaves the served set
    fake.now = 0.99
    assert cache.violations() == []
    fake.now = 1.0
    (problem,) = cache.violations()
    assert problem.startswith("profile two: GLASSES_ALLOW_ALL_USERS is set")


def test_a_profile_leaving_the_served_set_stops_being_read_after_a_second(install):  # noqa: F811 - the imported fixture is used by name
    hermes = install(Hermes({"one": {}, "two": OPEN}))
    fake = Counting()
    cache = gate.VerdictCache(clock=fake.clock)
    assert len(cache.violations()) == 1
    hermes.served = ["one"]
    fake.now = 1.0
    assert cache.violations() == []


# -- the event loop ------------------------------------------------------------------------------------------------


def test_concurrent_async_checks_share_one_recompute_and_the_loop_keeps_running(adapter_module, monkeypatch):  # noqa: F811 - the imported fixture is used by name
    calls = counting_sources(monkeypatch)
    inner = gate.sources
    ticked = threading.Event()  # set by the ticker once the loop has run five times

    def slow():  # runs in the adapter's worker thread; a blocked loop never sets the event
        ticked.wait(5)
        return inner()

    monkeypatch.setattr(gate, "sources", slow)
    adapter = new_adapter(adapter_module)

    async def main():
        ticks = 0
        stop = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                if ticks == 5:
                    ticked.set()
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        answers = await asyncio.gather(*(adapter.gate_ok_async() for _ in range(30)))
        stop.set()
        await task
        return answers, ticks

    answers, ticks = asyncio.run(main())
    assert answers == [True] * 30 and calls["n"] == 1
    assert ticks >= 5, ticks  # the recompute waits for five loop ticks, not for the clock; a blocked loop ticks 0 times


def test_gate_ok_async_leaves_no_verdict_when_the_check_fails(adapter_module, monkeypatch):  # noqa: F811 - the imported fixture is used by name
    state = {"fail": True}

    def sources():
        if state["fail"]:
            raise ZeroDivisionError
        return []

    monkeypatch.setattr(gate, "sources", sources)
    adapter = new_adapter(adapter_module)
    assert asyncio.run(adapter.gate_ok_async()) is False
    state["fail"] = False
    assert asyncio.run(adapter.gate_ok_async()) is True


# -- the clock -----------------------------------------------------------------------------------------------------


def test_the_repr_of_a_stored_verdict_holds_no_switch_value(monkeypatch):
    monkeypatch.setenv("GLASSES_ALLOWED_USERS", SENTINEL)
    cache = gate.VerdictCache(compute=lambda: [])
    cache.violations()
    assert cache._verdict is not None and SENTINEL not in repr(cache._verdict)


def test_a_verdict_is_valid_one_second_from_the_start_of_its_recompute_not_from_its_end():
    # A recompute that takes 0.9 s (several .env reads on a slow disk) must not extend the verdict's life: it is
    # stamped with the clock reading taken before the recompute, so a change can sit unseen for 1 s at most.
    fake = Counting()
    fast = fake.compute

    def slow_compute():
        problems = fast()
        fake.now += 0.9
        return problems

    fake.compute = slow_compute
    cache = fake.cache()
    cache.violations()  # starts at 0.0, ends at 0.9
    fake.now = 0.95
    cache.violations()
    assert fake.computed == 1  # 0.95 s after its start: still valid
    fake.now = 1.0
    cache.violations()
    assert fake.computed == 2  # exactly one second after its start: expired (stamped at the end it would be served)
