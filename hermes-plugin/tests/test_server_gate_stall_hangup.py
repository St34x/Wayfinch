# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b) over a real socket run by ``build_runner`` (handlers are not cancelled on a hang-up): a gate
that never answers is the fixed 503 within ``GATE_TIMEOUT``, and a client that hangs up while the gate waits is not an
admission when the gate answers later: no session, no dispatch, no error log, no slot kept."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from server_auth_helpers import FP, SpyRegistry
from server_helpers import app_for, raw, serving, wiring

from glasses import server_auth
from glasses.server import _AUTH

from test_server_hop_hangup_and_close import until, upgrade_request

REQUEST = upgrade_request("t" * 43, FP)


def test_a_gate_that_never_answers_is_the_fixed_503_within_the_timeout_on_a_real_socket(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(server_auth, "GATE_TIMEOUT", 0.4)
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    spy = SpyRegistry()
    asked: list[str] = []

    async def never() -> bool:
        await asyncio.Event().wait()
        return True

    def revoked(_id: str) -> bool:
        asked.append("is_revoked")
        return False

    async def main() -> tuple[bytes, float]:
        async with serving(app_for(w, gate_ok_async=never, registry=spy, is_revoked=revoked)) as port:
            started = time.monotonic()
            reply = await raw(port, REQUEST)
            return reply, time.monotonic() - started

    reply, elapsed = asyncio.run(asyncio.wait_for(main(), 20))
    assert reply.startswith(b"HTTP/1.1 503 ") and reply.endswith(b'{"error":"unavailable"}')
    assert 0.35 <= elapsed < 2.0 and spy.calls == [] and asked == [] and w.frames == []
    assert "TimeoutError" in caplog.text and "t" * 43 not in caplog.text


def hang_up_while_the_gate_waits(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> tuple[int, list[Any], int]:
    """A valid client hangs up at the gate; the gate then answers True. Returns (slots held, dispatched, reads)."""
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    spy = SpyRegistry(device=w.device)  # a valid token: if the late handler went on, it would be an admission
    answer = asyncio.Event()
    waiting: list[str] = []

    async def gate() -> bool:
        waiting.append("gate")
        await answer.wait()
        return True

    async def main() -> int:
        app: Any = app_for(w, gate_ok_async=gate, registry=spy)
        async with serving(app) as port:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(REQUEST)
            await writer.drain()
            await until(lambda: waiting == ["gate"], "the request to reach the gate")
            writer.close()
            await asyncio.sleep(0.2)
            answer.set()  # the gate opens after the client is gone
            await asyncio.sleep(0.5)
            pending: int = app[_AUTH].hop._pending
            return pending

    pending = asyncio.run(asyncio.wait_for(main(), 20))
    return pending, list(w.frames), len(spy.calls)


def test_a_client_that_hangs_up_while_the_gate_waits_is_not_admitted_when_the_gate_answers_later(tmp_path, caplog):
    pending, frames, reads = hang_up_while_the_gate_waits(tmp_path, caplog)
    assert pending == 0 and frames == [] and reads <= 1


def test_a_client_that_hangs_up_before_the_upgrade_completes_leaves_no_error_record_or_stack_trace(tmp_path, caplog):
    hang_up_while_the_gate_waits(tmp_path, caplog)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text
    assert "Traceback" not in caplog.text
    assert '" 500 ' not in caplog.text and "ConnectionResetError" not in "".join(
        r.getMessage() for r in caplog.records if r.levelno > logging.DEBUG
    )
