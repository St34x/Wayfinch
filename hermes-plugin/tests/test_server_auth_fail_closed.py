# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a), fail closed: a failing registry or revocation check is one of the fixed JSON
answers, never an HTTP 500 with a traceback; ``is_revoked`` answers ``False`` (admit) or ``True`` (403 marker), any
other answer is the 503 (the phone wipes only on a determined revocation)."""

from __future__ import annotations

import gc
import logging

import pytest
from server_auth_helpers import FP, SpyRegistry, creds, deps_for, enrolled, get, make_app, refusal

from glasses.server_auth import AuthDeps

UNAUTHORIZED = (401, "unauthorized")
UNAVAILABLE = (503, "unavailable")
REVOKED = (403, "device_revoked")
DETAIL = "detail-that-must-stay-out-of-every-log-line"


@pytest.mark.parametrize(
    "error",
    [ValueError(DETAIL), KeyError(DETAIL), RuntimeError(DETAIL), RecursionError(DETAIL), MemoryError(DETAIL)],
    ids=lambda error: type(error).__name__,
)
def test_a_registry_failure_of_any_exception_type_is_the_fixed_401(error, caplog):
    handled: list[str] = []
    caplog.set_level(logging.DEBUG)
    reply = get(make_app(deps_for(SpyRegistry(error=error)), handled), creds("t" * 43))
    assert refusal(reply, *UNAUTHORIZED), reply
    assert handled == []
    assert DETAIL not in caplog.text and "Traceback" not in caplog.text
    assert type(error).__name__ in caplog.text


class Sentinel:
    def __repr__(self) -> str:
        return DETAIL


@pytest.mark.parametrize("answer", [None, 0, "", [], 2, Sentinel()], ids=lambda answer: type(answer).__name__)
def test_an_answer_other_than_a_boolean_from_the_revocation_check_is_503_not_a_wipe(tmp_path, answer, caplog):
    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []
    caplog.set_level(logging.DEBUG)
    deps = AuthDeps(gate_ok_async=deps_for(registry).gate_ok_async, registry=registry, is_revoked=lambda _id: answer)  # type: ignore[arg-type, return-value]
    reply = get(make_app(deps, handled), creds(token))
    assert refusal(reply, *UNAVAILABLE), reply
    assert handled == []
    assert type(answer).__name__ in caplog.text
    assert DETAIL not in caplog.text and token not in caplog.text and FP not in caplog.text


def test_an_exact_true_from_the_revocation_check_is_the_only_403_of_the_check(tmp_path):
    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []
    deps = AuthDeps(gate_ok_async=deps_for(registry).gate_ok_async, registry=registry, is_revoked=lambda _id: True)
    assert refusal(get(make_app(deps, handled), creds(token)), *REVOKED)
    assert handled == []


def test_a_coroutine_function_as_the_revocation_check_is_503_and_nothing_is_left_unawaited(tmp_path, recwarn):
    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []

    async def wrongly_async(device_id: str) -> bool:
        return True

    deps = AuthDeps(
        gate_ok_async=deps_for(registry).gate_ok_async,
        registry=registry,
        is_revoked=wrongly_async,  # type: ignore[arg-type]
    )
    assert refusal(get(make_app(deps, handled), creds(token)), *UNAVAILABLE)
    assert handled == []
    gc.collect()
    assert [str(w.message) for w in recwarn if "never awaited" in str(w.message)] == []


def test_a_raising_revocation_check_is_the_fixed_503_and_admits_nobody(tmp_path, caplog):
    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []

    def broken(device_id: str) -> bool:
        raise RuntimeError(DETAIL)

    caplog.set_level(logging.DEBUG)
    deps = AuthDeps(gate_ok_async=deps_for(registry).gate_ok_async, registry=registry, is_revoked=broken)
    reply = get(make_app(deps, handled), creds(token))
    assert refusal(reply, *UNAVAILABLE), reply
    assert handled == []
    assert DETAIL not in caplog.text and "Traceback" not in caplog.text
    assert "RuntimeError" in caplog.text
