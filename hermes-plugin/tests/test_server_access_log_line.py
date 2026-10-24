# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), the access-log line: ``remote "METHOD path" status ms`` (no query string, integer
milliseconds), so a slow ``503`` of a stalled gate or registry shows in the log."""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp
from server_auth_helpers import creds
from server_helpers import app_for, serving, wiring

LINE = re.compile(r'127\.0\.0\.1 "GET /ws" 401 (\d+)')


async def slow_gate() -> bool:
    await asyncio.sleep(0.3)
    return True


def access_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]


def test_the_access_line_ends_with_the_elapsed_milliseconds_of_a_request_that_waited_in_the_gate(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    w = wiring(tmp_path)

    async def main() -> int:
        async with serving(app_for(w, gate_ok_async=slow_gate)) as port, aiohttp.ClientSession() as session:
            response = await session.get(f"http://127.0.0.1:{port}/ws", headers=creds("t" * 43))
            async with response:
                return response.status

    assert asyncio.run(asyncio.wait_for(main(), 20)) == 401
    lines = access_lines(caplog)
    assert len(lines) == 1
    match = LINE.fullmatch(lines[0])
    assert match is not None, lines
    assert 300 <= int(match.group(1)) < 5_000


def test_a_fast_request_has_a_small_integer_and_the_query_string_is_still_left_out(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    w = wiring(tmp_path)

    async def main() -> None:
        async with serving(app_for(w)) as port, aiohttp.ClientSession() as session:
            response = await session.get(f"http://127.0.0.1:{port}/ws?token=Qx7Sentinel", headers=creds("t" * 43))
            response.release()

    asyncio.run(asyncio.wait_for(main(), 20))
    lines = access_lines(caplog)
    assert len(lines) == 1 and "Qx7Sentinel" not in lines[0] and "?" not in lines[0]
    match = LINE.fullmatch(lines[0])
    assert match is not None, lines
    # one stalled turn of a loaded host must not fail this test; the test above pins the >= 300 ms side
    assert 0 <= int(match.group(1)) < 5000
