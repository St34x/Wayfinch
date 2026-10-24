# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication on the adapter's real allow-all gate check (the T1.209 bound): while the gate's recompute
is stuck on a blocked read, the first upgrade request is answered 503 after about ``LOCK_TIMEOUT`` + 1 seconds, the
next ones at once, no registry file is read, and the event loop keeps serving other requests.

Real clock with the shipped constants: the first case takes about 3 s.
"""

from __future__ import annotations

import asyncio
import threading
import time

from aiohttp.test_utils import TestClient, TestServer
from server_auth_helpers import SpyRegistry, creds, enrolled, fetch, make_app, refusal

from glasses import gate
from glasses.server_auth import AuthDeps

from test_adapter import new_adapter
from test_gate import adapter_module  # noqa: F401 - fixture

BOUND = gate.LOCK_TIMEOUT + 1.5


def stalled(adapter) -> threading.Event:
    """Make the adapter's gate recompute block until the returned event is set (a 30 s cap ends it anyway)."""
    release = threading.Event()

    def compute() -> list[str]:
        release.wait(30)
        return []

    adapter._gate_cache = gate.VerdictCache(compute=compute)
    return release


def test_an_upgrade_on_a_stuck_gate_recompute_is_503_in_about_three_seconds_and_the_loop_stays_free(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
):
    adapter = new_adapter(adapter_module)
    release = stalled(adapter)
    spy = SpyRegistry()
    handled: list[str] = []
    deps = AuthDeps(gate_ok_async=adapter.gate_ok_async, registry=spy, is_revoked=lambda device_id: False)

    async def main():
        app = make_app(deps, handled)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            pings = 0

            async def ping_while_waiting() -> None:
                nonlocal pings
                while True:
                    async with client.get("/ping") as response:
                        assert await response.text() == "pong"
                    pings += 1
                    await asyncio.sleep(0.05)

            pinger = asyncio.create_task(ping_while_waiting())
            started = time.monotonic()
            first = await fetch(client, creds("t" * 43))
            first_waited = time.monotonic() - started
            started = time.monotonic()
            second = await fetch(client, creds("t" * 43))  # the read is still blocked: refused at once
            second_waited = time.monotonic() - started
            pinger.cancel()
            return first, first_waited, second, second_waited, pings

    try:
        first, first_waited, second, second_waited, pings = asyncio.run(main())
    finally:
        release.set()
        asyncio.run(adapter.disconnect())
    assert refusal(first, 503, "unavailable") and refusal(second, 503, "unavailable")
    assert gate.LOCK_TIMEOUT - 0.2 <= first_waited <= BOUND, first_waited
    assert second_waited < 0.5, second_waited
    assert pings >= 20, pings  # /ping answered about every 50 ms during the 3 s wait
    assert spy.calls == [] and handled == []


def test_a_clean_gate_after_the_stall_ends_admits_again(
    adapter_module,  # noqa: F811 - the imported fixture is used by name
    tmp_path,
):
    adapter = new_adapter(adapter_module)
    adapter._gate_cache = gate.VerdictCache(compute=lambda: [])
    registry, device, token = enrolled(tmp_path)
    handled: list[str] = []
    deps = AuthDeps(gate_ok_async=adapter.gate_ok_async, registry=registry, is_revoked=lambda device_id: False)

    async def main() -> int:
        async with TestClient(TestServer(make_app(deps, handled), host="127.0.0.1")) as client:
            return (await fetch(client, creds(token))).status

    try:
        assert asyncio.run(main()) == 101
    finally:
        asyncio.run(adapter.disconnect())
    assert handled == [device.device_id]
