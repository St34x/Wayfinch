# SPDX-License-Identifier: Apache-2.0
"""WS server ``/enroll`` mount edge cases (T1.07c): a refused peer learns nothing whatever it sends, and shutdown
with an enrolment in flight."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import Any

import aiohttp
import enroll_helpers
import pytest
from enroll_helpers import BODY, factory
from fake_signer import Script
from server_helpers import app_for, serving, wiring

fake = enroll_helpers.fake


def run(coro: Awaitable[Any], limit: float = 60) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))  # type: ignore[arg-type]


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
@pytest.mark.parametrize("path", ["/enroll", "/enroll/", "/enroll?x=1"])
def test_a_refused_peer_gets_the_status_of_an_unknown_path_for_any_method_and_spelling(tmp_path, fake, method, path):
    w = wiring(tmp_path)
    w.allow_peer = False

    async def main() -> tuple[int, int]:
        async with serving(app_for(w, signer_client=factory(fake))) as port, aiohttp.ClientSession() as session:
            async with session.request(method, f"http://127.0.0.1:{port}{path}", json=BODY) as response:
                got = response.status
            async with session.request(method, f"http://127.0.0.1:{port}/nope", json=BODY) as response:
                return got, response.status

    got, unknown = run(main())
    assert got == unknown == 404 and fake.requests == []


def test_a_refused_peer_sending_an_oversized_or_wrongly_typed_body_gets_404_not_413_or_400(tmp_path, fake):
    w = wiring(tmp_path)
    w.allow_peer = False
    big = b"{" + b" " * 9000 + b"}"

    async def main() -> list[int]:
        statuses = []
        async with serving(app_for(w, signer_client=factory(fake))) as port, aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{port}/enroll"
            for options in (
                {"data": big, "headers": {"Content-Type": "application/json"}},
                {"data": b"not json", "headers": {"Content-Type": "text/plain"}},
                {"data": b"x", "headers": {"Content-Type": "application/json", "Content-Length": "1"}},
            ):
                async with session.post(url, **options) as response:
                    statuses.append(response.status)
        return statuses

    assert run(main()) == [404, 404, 404] and fake.requests == []  # the peer check precedes every body check


def test_an_enrolment_in_flight_at_shutdown_finishes_and_shutdown_waits_for_it_not_60_seconds(tmp_path, fake):
    fake.scripts["enrol"] = Script(delay_s=1.0)
    w = wiring(tmp_path)

    async def main() -> tuple[int, float]:
        started = 0.0
        async with aiohttp.ClientSession() as session:
            async with serving(app_for(w, signer_client=factory(fake, enrol_s=3.0))) as port:
                call = asyncio.create_task(session.post(f"http://127.0.0.1:{port}/enroll", json=BODY))
                while not fake.requests:
                    await asyncio.sleep(0.01)
                started = time.monotonic()
            took = time.monotonic() - started
            async with await call as response:
                return response.status, took

    status, took = run(main())
    assert status in (200, 503) and took < 5  # the signer's own delay, bounded by the client timeout: never the 60 s
