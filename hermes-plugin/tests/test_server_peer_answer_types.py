# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), the answer of ``peer_allowed``: only an exact ``True`` admits a peer. Any other answer (a
truthy number or string, ``None``, a mock) is a refused peer and gets the unknown-path ``404`` on ``/ws`` and on
``/enroll``, with no gate, registry or revocation call behind it."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import aiohttp
import pytest
from server_auth_helpers import always
from server_helpers import app_for, serving, wiring


@pytest.mark.parametrize(
    "answer",
    [1, "yes", [0], None, 0, "", Mock(), object()],
    ids=["one", "yes", "list", "none", "zero", "empty", "mock", "object"],
)
def test_a_peer_check_that_answers_something_other_than_true_is_a_refused_peer(tmp_path, answer):
    w = wiring(tmp_path)
    calls: list[str] = []

    async def gate() -> bool:
        calls.append("gate")
        return True

    def is_revoked(device_id: str) -> bool:
        calls.append("is_revoked")
        return False

    def peer_allowed(request: Any) -> Any:
        return answer

    async def main() -> list[int]:
        deps = {"peer_allowed": peer_allowed, "gate_ok_async": gate, "is_revoked": is_revoked}
        async with serving(app_for(w, **deps)) as port, aiohttp.ClientSession() as session:
            statuses = []
            for method, path in (("GET", "/ws"), ("POST", "/ws"), ("POST", "/enroll"), ("GET", "/enroll")):
                async with session.request(method, f"http://127.0.0.1:{port}{path}", headers=w.headers()) as response:
                    statuses.append(response.status)
            return statuses

    assert asyncio.run(asyncio.wait_for(main(), 30)) == [404, 404, 404, 404]
    assert calls == [] and w.frames == []


def test_the_exact_true_still_admits_the_peer(tmp_path):
    w = wiring(tmp_path)

    async def main() -> int:
        app = app_for(w, peer_allowed=lambda request: True, gate_ok_async=always(True))
        async with (
            serving(app) as port,
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{port}/ws") as response,
        ):
            return response.status

    assert asyncio.run(asyncio.wait_for(main(), 30)) == 401  # past the peer check: the missing credentials decide
