# SPDX-License-Identifier: Apache-2.0
"""The allow-all gate's verdict key: the caller's active-scope values are part of it, and it is read inside the lock.

Hermes keeps the active secret scope in a context variable, so the cache (shared by every caller of one adapter) must
not serve the verdict of a caller with a clean scope to a caller whose scope holds a switch. The fake
``platform_gate_env`` here is backed by a ``ContextVar`` and called through ``contextvars.copy_context().run``; fakes
only, no Hermes import. The real-Hermes half is test_gate_active_scope_real.py.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextvars import ContextVar, copy_context
from types import SimpleNamespace
from typing import Any

import pytest

from glasses import gate

from test_adapter import new_adapter
from test_gate import SENTINEL, Hermes, adapter_module, install  # noqa: F401 - fixtures

SCOPE: ContextVar[dict[str, str] | None] = ContextVar("scope", default=None)
OPEN = {"GLASSES_ALLOW_ALL_USERS": "1"}
SWITCH_VALUES = [
    ("GLASSES_ALLOW_ALL_USERS", "1"),
    ("GLASSES_ALLOWED_USERS", "dev_1,*"),
    ("GATEWAY_ALLOWED_USERS", "*"),
    ("GATEWAY_ALLOW_ALL_USERS", "true"),
]


class ScopedHermes(Hermes):
    """Stand-in Hermes whose ``platform_gate_env`` reads the scope installed in the caller's context."""

    def __init__(self, served):
        super().__init__(served)
        self.error: BaseException | None = None

    def gate_env(self, name: str, default: str = "") -> str:
        if self.error is not None:
            raise self.error
        return ((SCOPE.get() or {}).get(name) or default).strip()

    def modules(self):
        return {**super().modules(), "gateway.platforms._shared": SimpleNamespace(platform_gate_env=self.gate_env)}


def in_scope(values: dict[str, str], call):
    """``call()`` in a copy of the current context with ``values`` installed as the active scope."""

    def run():
        SCOPE.set(values)
        return call()

    return copy_context().run(run)


class Counted:
    """The real gate as the cache's ``compute``, counting recomputes; the clock stays where the test leaves it."""

    def __init__(self):
        self.computed = 0
        self.now = 0.0

    def compute(self):
        self.computed += 1
        return gate.violations()

    def cache(self):
        return gate.VerdictCache(clock=lambda: self.now, compute=self.compute)


@pytest.fixture
def hermes(install):  # noqa: F811 - the imported fixture is used by name
    return install(ScopedHermes({"dev": {}}))


@pytest.mark.parametrize(("name", "value"), SWITCH_VALUES)
@pytest.mark.parametrize("clean_first", [True, False])
def test_a_caller_with_a_switch_in_its_scope_is_refused_at_once_after_a_clean_caller(hermes, name, value, clean_first):
    fake = Counted()
    cache = fake.cache()
    refused = in_scope({name: value}, gate.violations)  # what a recompute in that context says
    assert len(refused) == 1 and refused[0].startswith(f"active scope: {name}")
    if clean_first:
        assert in_scope({}, cache.violations) == []
    assert in_scope({name: value}, cache.violations) == refused  # same second, same clock reading
    assert in_scope({}, cache.violations) == []  # and the clean caller is not served the refusal
    assert fake.computed == (3 if clean_first else 2)  # one verdict is kept: each change of caller recomputes


def test_a_thousand_calls_from_one_scoped_context_inside_a_second_recompute_once(hermes):
    fake = Counted()
    cache = fake.cache()
    answers = in_scope(OPEN, lambda: [cache.violations() for _ in range(1000)])
    assert all(answer == answers[0] and len(answer) == 1 for answer in answers)
    assert fake.computed == 1


def test_callers_with_equal_scope_values_share_one_verdict(hermes):
    fake = Counted()
    cache = fake.cache()
    answers = [in_scope(dict(OPEN), cache.violations) for _ in range(5)]  # five contexts, five equal mappings
    assert answers == [answers[0]] * 5 and len(answers[0]) == 1
    assert fake.computed == 1


def test_the_scope_value_never_reaches_a_message(hermes):
    cache = Counted().cache()
    scope = {"GLASSES_ALLOW_ALL_USERS": SENTINEL, "GLASSES_ALLOWED_USERS": f"{SENTINEL},*"}
    problems = in_scope(scope, cache.violations)
    assert problems and not any(SENTINEL in problem for problem in problems), problems


def test_a_changed_process_switch_still_recomputes_at_once(hermes, monkeypatch):
    fake = Counted()
    cache = fake.cache()
    assert in_scope({}, cache.violations) == []
    monkeypatch.setenv("GLASSES_ALLOWED_USERS", "*")
    (problem,) = in_scope({}, cache.violations)
    assert problem.startswith("process environment: GLASSES_ALLOWED_USERS")
    assert fake.computed == 2


# -- an error out of the scope reader ------------------------------------------------------------------------------


def test_an_unreadable_scope_is_a_refusal_without_values_that_is_not_stored(hermes):
    fake = Counted()
    cache = fake.cache()
    hermes.error = OSError(SENTINEL)
    (problem,) = cache.violations()
    assert problem.startswith("profile scopes unreadable (OSError)") and SENTINEL not in problem
    assert fake.computed == 0  # no recompute behind a key that cannot be read
    hermes.error = None
    assert cache.violations() == []  # nothing was stored: the next call is judged at once


@pytest.mark.parametrize("exc", [ZeroDivisionError(SENTINEL), AssertionError(SENTINEL), StopIteration(SENTINEL)])
def test_an_unexpected_error_out_of_the_scope_reader_propagates_and_leaves_no_verdict(hermes, exc):
    fake = Counted()
    cache = fake.cache()
    assert cache.violations() == []
    hermes.error = exc
    fake.now = 1.0
    with pytest.raises(type(exc)):
        cache.violations()
    hermes.error = None
    assert in_scope(OPEN, cache.violations)  # not served from the verdict kept before the error


# -- the adapter's async path --------------------------------------------------------------------------------------


def test_the_async_gate_check_reads_the_active_scope_of_the_awaiting_context(hermes, adapter_module):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)

    async def ask(values: dict[str, str]) -> bool:
        SCOPE.set(values)  # installed in the task's context; to_thread must copy it into the worker
        return await adapter.gate_ok_async()

    assert copy_context().run(asyncio.run, ask({})) is True
    assert copy_context().run(asyncio.run, ask(OPEN)) is False  # same adapter, same second
    assert copy_context().run(asyncio.run, ask({})) is True


# -- the key is read inside the lock -------------------------------------------------------------------------------


class ArrivalSpy:
    """Wraps the cache's lock; ``arrived`` is set when a second caller reaches it."""

    def __init__(self, inner: Any, arrived: threading.Event):
        self.inner, self.arrived, self.calls = inner, arrived, 0

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        self.calls += 1
        if self.calls == 2:
            self.arrived.set()
        return self.inner.acquire(*args, **kwargs)

    def release(self) -> None:
        self.inner.release()

    def __enter__(self) -> ArrivalSpy:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def test_a_caller_that_waited_stores_its_verdict_under_the_key_it_read_after_the_lock(monkeypatch):
    # The first caller blocks inside its recompute. The second reaches the lock, then the process switch flips and
    # the first verdict expires. Had the second read its key before waiting it would store a clean verdict under the
    # key of the OPEN switch, and the next caller with the switch open would be served that clean verdict.
    switch = "GLASSES_ALLOW_ALL_USERS"
    monkeypatch.setenv(switch, "1")
    inside, release, arrived = threading.Event(), threading.Event(), threading.Event()
    clock, computes = [0.0], []

    def compute():
        computes.append(1)
        if len(computes) == 1:
            inside.set()
            assert release.wait(5)
        return ["process environment: bad"] if os.environ.get(switch) else []

    cache = gate.VerdictCache(clock=lambda: clock[0], compute=compute)
    cache._lock = ArrivalSpy(cache._lock, arrived)  # type: ignore[assignment]
    first = threading.Thread(target=cache.violations)
    first.start()
    assert inside.wait(5)
    second = threading.Thread(target=cache.violations)
    second.start()
    assert arrived.wait(5)
    monkeypatch.delenv(switch)
    clock[0] = 2.0
    release.set()
    for thread in (first, second):
        thread.join(5)
        assert not thread.is_alive()
    monkeypatch.setenv(switch, "1")
    assert cache.violations() == ["process environment: bad"]
    assert len(computes) == 3
