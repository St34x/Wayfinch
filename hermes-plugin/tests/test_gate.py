# SPDX-License-Identifier: Apache-2.0
"""Scope of the allow-all gate (``glasses.gate``) and its use by the adapter.

The gate reads the profiles this process serves (``profiles_to_serve(True)``), not every installed profile. The
verdict cache is in test_gate_cache.py and test_gate_cache_edges.py. No Hermes import (conftest.py enforces it): the
Hermes readers are stand-in modules in ``sys.modules`` for the real ``gate._hermes_api`` wiring, or fake callables.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import threading
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace

import pytest

from glasses import gate
from glasses.config import environ_reader

from test_adapter import (
    ADAPTER_MODULE,
    ROUTER_MODULE,
    _fake_hermes_modules,
    _forget_adapter_modules,
    event,
    new_adapter,
)

SENTINEL = "sentinel-value-7291"  # must never appear in a message
OPEN = {"GLASSES_ALLOW_ALL_USERS": "true"}


class Hermes:
    """Stand-in Hermes: ``served`` profiles, plus ``parked`` and ``standalone`` ones that only the wide call returns."""

    def __init__(self, served, parked=None, standalone=None):
        self.scopes = {**served, **(parked or {}), **(standalone or {})}
        self.served = list(served)
        self.extra = list(parked or {}) + list(standalone or {})
        self.calls: list[tuple[bool, bool, bool]] = []

    def profiles_to_serve(self, multiplex, *, include_standalone=False, include_parked=False):
        self.calls.append((multiplex, include_standalone, include_parked))
        names = self.served + (self.extra if include_standalone or include_parked else [])
        return [(name, f"/fake/{name}") for name in names]

    def modules(self):
        return {
            "gateway.platforms._shared": SimpleNamespace(platform_gate_env=lambda name, default="": default),
            "agent.secret_scope": SimpleNamespace(build_profile_secret_scope=lambda home: self.scopes[home.name]),
            "hermes_cli.profiles": SimpleNamespace(profiles_to_serve=self.profiles_to_serve),
        }


def install_hermes(mp: pytest.MonkeyPatch, hermes: Hermes) -> Hermes:
    """Put the stand-ins of ``hermes`` in ``sys.modules`` until ``mp`` is undone."""
    for name, module in hermes.modules().items():
        mp.setitem(sys.modules, name, module)
    for parent in ("gateway", "gateway.platforms", "agent", "hermes_cli"):
        mp.setitem(sys.modules, parent, sys.modules.get(parent) or SimpleNamespace())
    return hermes


@pytest.fixture
def install():
    """``install(Hermes(...))`` puts the stand-ins in ``sys.modules`` until this test ends."""
    with pytest.MonkeyPatch.context() as mp:
        yield lambda hermes: install_hermes(mp, hermes)


# --------------------------------------------------------------------------------------------
# scope: the served profiles only
# --------------------------------------------------------------------------------------------


def test_the_gate_asks_hermes_for_the_served_profiles_only(install):
    hermes = install(Hermes({"default": {}, "dev": {}}))
    labels = [label for label, _ in gate.sources()]
    assert labels == ["process environment", "active scope", "profile default", "profile dev"]
    assert hermes.calls == [(True, False, False)]


@pytest.mark.parametrize("kind", ["parked", "standalone"])
def test_a_parked_or_standalone_profile_with_allow_all_does_not_refuse(install, kind):
    install(Hermes({"default": {}}, **{kind: {"beta": OPEN}}))
    assert gate.violations() == []


def test_a_served_profile_with_allow_all_still_refuses(install):
    install(Hermes({"default": {}, "dev": OPEN}, parked={"beta": OPEN}))
    (problem,) = gate.violations()
    assert problem.startswith("profile dev: GLASSES_ALLOW_ALL_USERS is set")


def test_a_wildcard_allowlist_in_a_served_profile_still_refuses(install):
    install(Hermes({"default": {"GLASSES_ALLOWED_USERS": "dev_1,*"}}))
    (problem,) = gate.violations()
    assert problem.startswith("profile default: GLASSES_ALLOWED_USERS contains a wildcard")


def test_a_profile_problem_says_the_switch_can_be_removed_from_that_profile(install):
    install(Hermes({"dev": OPEN}))
    (problem,) = gate.violations()
    assert problem.endswith("or remove the switch from that profile")


def test_the_process_environment_problem_names_no_profile(monkeypatch):
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", "1")
    (problem,) = gate.violations()
    assert problem.startswith("process environment: GLASSES_ALLOW_ALL_USERS is set")
    assert "profile" not in problem.split(";", 1)[1]


def test_no_value_reaches_a_message(install, monkeypatch):
    monkeypatch.setenv("GLASSES_ALLOWED_USERS", f"{SENTINEL},*")
    install(Hermes({"dev": {"GLASSES_ALLOW_ALL_USERS": SENTINEL, "GATEWAY_ALLOWED_USERS": f"*,{SENTINEL}"}}))
    problems = gate.violations()
    assert len(problems) >= 3
    assert not any(SENTINEL in p for p in problems), problems


# --------------------------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------------------------


@pytest.fixture
def adapter_module() -> Iterator[ModuleType]:
    """``glasses.adapter`` imported against the stand-in gateway modules of test_adapter.py."""
    with pytest.MonkeyPatch.context() as mp:
        for name, module in _fake_hermes_modules().items():
            mp.setitem(sys.modules, name, module)
        for name in (ADAPTER_MODULE, ROUTER_MODULE):  # both import the stand-in gateway modules
            mp.delitem(sys.modules, name, raising=False)
        try:
            yield importlib.import_module(ADAPTER_MODULE)
        finally:
            _forget_adapter_modules()


def counting_sources(monkeypatch, scope=None):
    calls = {"n": 0, "threads": set()}

    def sources():
        calls["n"] += 1
        calls["threads"].add(threading.get_ident())
        return [("process environment", environ_reader()), ("profile dev", lambda name: (scope or {}).get(name, ""))]

    monkeypatch.setattr(gate, "sources", sources)
    return calls


def freeze_clock(monkeypatch):
    """The verdict cache of adapters built from now on reads a clock that never moves: no timing margin."""
    monkeypatch.setattr(gate, "time", SimpleNamespace(monotonic=lambda: 0.0))


def test_gate_ok_does_a_thousand_checks_with_one_recompute(adapter_module, monkeypatch):
    calls = counting_sources(monkeypatch)
    freeze_clock(monkeypatch)
    adapter = new_adapter(adapter_module)
    assert all(adapter.gate_ok() for _ in range(1000))
    assert calls["n"] == 1


def test_gate_ok_refuses_while_a_served_scope_is_open_and_logs_no_value(adapter_module, monkeypatch, caplog):
    counting_sources(monkeypatch, {"GLASSES_ALLOW_ALL_USERS": SENTINEL})
    adapter = new_adapter(adapter_module)
    with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
        assert adapter.gate_ok() is False
        assert adapter.gate_ok() is False
    assert "refusing: profile dev: GLASSES_ALLOW_ALL_USERS is set" in caplog.text
    assert SENTINEL not in caplog.text


def test_gate_ok_async_gives_the_same_answer_in_a_worker_thread(adapter_module, monkeypatch):
    calls = counting_sources(monkeypatch)
    adapter = new_adapter(adapter_module)
    assert asyncio.run(adapter.gate_ok_async()) is True
    assert calls["threads"] and threading.get_ident() not in calls["threads"]
    monkeypatch.setenv("GLASSES_ALLOWED_USERS", "*")
    assert asyncio.run(adapter.gate_ok_async()) is False


def test_gate_ok_async_does_not_block_the_event_loop(adapter_module, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def sources():
        started.set()
        assert release.wait(5), "the loop never ran while the check was blocked"
        return []

    monkeypatch.setattr(gate, "sources", sources)
    adapter = new_adapter(adapter_module)

    async def main():
        check = asyncio.create_task(adapter.gate_ok_async())
        while not started.is_set():
            await asyncio.sleep(0)
        release.set()  # only reachable if the loop is free while the check runs
        return await check

    assert asyncio.run(main()) is True


def test_gate_ok_async_refuses_on_an_unexpected_error(adapter_module, monkeypatch, caplog):
    def boom():
        raise ZeroDivisionError(SENTINEL)

    monkeypatch.setattr(gate, "sources", boom)
    adapter = new_adapter(adapter_module)
    with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
        assert asyncio.run(adapter.gate_ok_async()) is False
    assert "allow-all gate check failed (ZeroDivisionError)" in caplog.text and SENTINEL not in caplog.text


def test_connect_and_events_use_the_cached_check(adapter_module, monkeypatch):
    calls = counting_sources(monkeypatch)
    freeze_clock(monkeypatch)
    adapter = new_adapter(adapter_module)
    asyncio.run(adapter.connect())
    for _ in range(50):
        asyncio.run(adapter.handle_message(event()))
    assert calls["n"] == 1
    assert len(adapter.handed_on) == 50


def test_each_adapter_has_its_own_verdict(adapter_module, monkeypatch):
    calls = counting_sources(monkeypatch)
    new_adapter(adapter_module).gate_ok()
    new_adapter(adapter_module).gate_ok()
    assert calls["n"] == 2
