# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a): ``authenticate`` bounds the allow-all gate check with a timeout of its own, so a
gate that never answers is the fixed 503 within ``GATE_TIMEOUT`` and nobody is admitted."""

from __future__ import annotations

import ast
import asyncio
import logging
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from server_auth_helpers import SpyRegistry, creds, fetch, make_app, refusal

from glasses import gate, server_auth
from glasses.server_auth import GATE_TIMEOUT, AuthDeps

TOKEN = "t" * 43


async def never() -> bool:
    await asyncio.Event().wait()
    return True


def test_the_timeout_is_a_literal_larger_than_the_adapters_own_bound():
    assert GATE_TIMEOUT == 5.0 and GATE_TIMEOUT > gate.LOCK_TIMEOUT + 1  # the adapter's refusal wins when it works
    source = Path(server_auth.__file__).read_text(encoding="utf-8")
    imported = {a.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom) for a in n.names}
    modules = {n.module for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)}
    assert "gate" not in modules and "gate" not in imported  # a constant of its own: gate.py is never imported here


def test_a_gate_that_never_answers_is_the_fixed_503_within_the_timeout_and_the_loop_keeps_ticking(monkeypatch, caplog):
    monkeypatch.setattr(server_auth, "GATE_TIMEOUT", 1.0)  # ideal 50 ticks of 20 ms; 5 show the loop ran
    caplog.set_level(logging.DEBUG)
    spy = SpyRegistry()
    handled: list[str] = []
    deps = AuthDeps(gate_ok_async=never, registry=spy, is_revoked=lambda _id: False)

    async def main() -> tuple[float, int]:
        async with TestClient(TestServer(make_app(deps, handled), host="127.0.0.1")) as client:
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            ticker = asyncio.create_task(tick())
            started = time.monotonic()
            reply = await fetch(client, creds(TOKEN))
            elapsed = time.monotonic() - started
            ticker.cancel()
            assert refusal(reply, 503, "unavailable"), reply
            return elapsed, ticks

    elapsed, ticks = asyncio.run(main())
    assert 0.95 <= elapsed < 4.0, elapsed
    assert ticks >= 5, ticks  # a loop blocked for the wait would have ticked 0 times
    assert spy.calls == [] and handled == []  # nothing admitted, the registry never read
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "glasses ws: allow-all gate check failed (TimeoutError)"
    ]
    assert TOKEN not in caplog.text


def test_a_request_cancelled_while_the_gate_waits_is_not_an_admission(monkeypatch):
    monkeypatch.setattr(server_auth, "GATE_TIMEOUT", 5.0)
    spy = SpyRegistry()
    handled: list[str] = []
    deps = AuthDeps(gate_ok_async=never, registry=spy, is_revoked=lambda _id: False)

    async def main() -> None:
        async with TestClient(TestServer(make_app(deps, handled), host="127.0.0.1")) as client:
            task = asyncio.create_task(fetch(client, creds(TOKEN)))
            await asyncio.sleep(0.2)
            task.cancel()  # the client hangs up
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.2)

    asyncio.run(main())
    assert spy.calls == [] and handled == []
