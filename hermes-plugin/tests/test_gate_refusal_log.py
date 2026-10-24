# SPDX-License-Identifier: Apache-2.0
"""What the adapter logs about the allow-all gate: one WARNING per change of the refusal, never one per call.

A refusal that persists would otherwise repeat the same line for every dropped frame and every reconnect attempt.
The adapter logs a refusal at WARNING when the set of problems differs from the one it logged last (the first
refusal, or a changed set), a return to clean once at INFO, and the dropped-event line at WARNING only for the first
drop of a refusal (DEBUG after). Texts are constants of the gate: no value reaches a log.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from glasses import gate

from test_adapter import ADAPTER_MODULE, event, new_adapter
from test_gate import SENTINEL, adapter_module, counting_sources  # noqa: F401 - fixtures

ALL = "GLASSES_ALLOW_ALL_USERS"
WILDCARD = "GLASSES_ALLOWED_USERS"


def _records(caplog, level: int | None = None, contains: str = "") -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == ADAPTER_MODULE and (level is None or r.levelno == level) and contains in r.getMessage()
    ]


def test_a_thousand_refused_calls_log_one_warning(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    monkeypatch.setenv(ALL, "1")
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        assert not any(adapter.gate_ok() for _ in range(1000))
    assert len(_records(caplog, logging.WARNING)) == 1
    assert f"refusing: process environment: {ALL}" in caplog.text


def test_a_changed_problem_set_logs_one_more_warning(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    monkeypatch.setenv(ALL, "1")
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        for _ in range(50):
            adapter.gate_ok()
        monkeypatch.delenv(ALL)
        monkeypatch.setenv(WILDCARD, "dev_1,*")  # another single problem: the set changed
        for _ in range(50):
            adapter.gate_ok()
    warnings = _records(caplog, logging.WARNING)
    assert [ALL in w.getMessage() for w in warnings] == [True, False]
    assert WILDCARD in warnings[1].getMessage()


def test_a_return_to_clean_logs_once_at_info_and_a_new_refusal_warns_again(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        assert all(adapter.gate_ok() for _ in range(20))
        assert _records(caplog) == []  # a clean gate that was never refused logs nothing
        monkeypatch.setenv(ALL, "1")
        assert not adapter.gate_ok()
        monkeypatch.delenv(ALL)
        assert all(adapter.gate_ok() for _ in range(20))
        assert len(_records(caplog, logging.INFO)) == 1
        monkeypatch.setenv(ALL, "1")
        assert not adapter.gate_ok()
    assert len(_records(caplog, logging.WARNING)) == 2
    assert len(_records(caplog, logging.INFO)) == 1


def test_an_unexpected_gate_error_repeated_on_every_call_is_logged_once_by_type(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    def boom():
        raise ZeroDivisionError(SENTINEL)

    monkeypatch.setattr(gate, "sources", boom)
    adapter = new_adapter(adapter_module)
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        assert not any(adapter.gate_ok() for _ in range(50))  # an error leaves no verdict: every call recomputes
    (record,) = _records(caplog, logging.WARNING)
    assert "allow-all gate check failed (ZeroDivisionError)" in record.getMessage()
    assert SENTINEL not in caplog.text


def test_dropped_events_warn_for_the_first_drop_only(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    monkeypatch.setenv(WILDCARD, "*")
    dropped = [event() for _ in range(100)]
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        for inbound in dropped:
            asyncio.run(adapter.handle_message(inbound))
    assert all(inbound._gateway_accepted is False for inbound in dropped) and adapter.handed_on == []
    lines = _records(caplog, contains="inbound event dropped")
    assert [r.levelno for r in lines] == [logging.WARNING] + [logging.DEBUG] * 99
    assert len(_records(caplog, logging.WARNING)) == 2  # the refusal itself and the first drop
    assert "hello" not in caplog.text


def test_a_new_refusal_after_a_clean_spell_warns_for_its_first_drop_again(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        for _ in range(2):
            monkeypatch.setenv(WILDCARD, "*")
            asyncio.run(adapter.handle_message(event()))
            asyncio.run(adapter.handle_message(event()))
            monkeypatch.delenv(WILDCARD)
            asyncio.run(adapter.handle_message(event()))
    levels = [r.levelno for r in _records(caplog, contains="inbound event dropped")]
    assert levels == [logging.WARNING, logging.DEBUG, logging.WARNING, logging.DEBUG]


def test_threads_asking_at_once_log_one_warning_per_change(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    monkeypatch.setenv(ALL, "1")
    start = threading.Barrier(8)

    def ask():
        start.wait(5)
        for _ in range(100):
            adapter.gate_ok()

    threads = [threading.Thread(target=ask) for _ in range(8)]
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
    assert len(_records(caplog, logging.WARNING)) == 1


def test_a_value_in_a_refused_scope_never_reaches_the_log(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture is used by name
    counting_sources(monkeypatch, {ALL: SENTINEL, WILDCARD: f"{SENTINEL},*"})
    adapter = new_adapter(adapter_module)
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        for _ in range(5):
            assert adapter.gate_ok() is False
            asyncio.run(adapter.handle_message(event()))
    assert caplog.text and SENTINEL not in caplog.text


@pytest.mark.parametrize("entry", ["gate_ok", "gate_ok_async"])
def test_the_timeout_refusal_is_one_constant_warning(adapter_module, caplog, entry):  # noqa: F811 - the imported fixture is used by name
    adapter = new_adapter(adapter_module)
    adapter._gate_cache = gate.VerdictCache(compute=lambda: [], lock_timeout=0.0)
    adapter._gate_cache._lock.acquire()  # held by "another caller" for the whole test

    def ask() -> list[bool]:
        call = adapter.gate_ok if entry == "gate_ok" else lambda: asyncio.run(adapter.gate_ok_async())
        return [call() for _ in range(20)]

    try:
        with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
            asker = threading.Thread(target=lambda: answers.extend(ask()))  # a lock wait without timeout never ends
            answers: list[bool] = []
            asker.start()
            asker.join(5)
            assert not asker.is_alive(), "the callers are still waiting for the lock"
    finally:
        adapter._gate_cache._lock.release()
    assert answers == [False] * 20
    (record,) = _records(caplog, logging.WARNING)
    assert record.getMessage().endswith(gate.LOCK_TIMEOUT_REFUSAL)
