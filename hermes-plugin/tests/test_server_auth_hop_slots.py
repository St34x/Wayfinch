# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a): the registry hop is the module's own pool, and a slot is given back
when a read finishes or fails, so a long run of wrong upgrades never ends in a permanent 503."""

from __future__ import annotations

import asyncio
import logging
import threading

from aiohttp.test_utils import TestClient, TestServer
from server_auth_helpers import FP, SpyRegistry, always, creds, deps_for, fetch, get, make_app, refusal

from glasses.registry import Device
from glasses.server_auth import HOP_PENDING_MAX, AuthDeps, RegistryHop

GOOD_TOKEN = "t" * 43


class ThreadSpy(SpyRegistry):
    """Records the name of the thread each registry read runs in."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[str] = []

    def lookup(self, token: object, fingerprint: object) -> tuple[Device | None, bool]:
        self.threads.append(threading.current_thread().name)
        return super().lookup(token, fingerprint)


def test_registry_reads_run_in_the_hops_own_threads_not_the_default_executor():
    spy = ThreadSpy()
    assert refusal(get(make_app(deps_for(spy), []), creds(GOOD_TOKEN)), 401, "unauthorized")
    assert len(spy.threads) == 1
    assert spy.threads[0].startswith("glasses-auth"), spy.threads


def test_a_long_run_of_wrong_upgrades_never_fills_the_hop():
    spy = SpyRegistry()
    runs = 3 * HOP_PENDING_MAX

    async def main() -> list[int]:
        async with TestClient(TestServer(make_app(deps_for(spy), []), host="127.0.0.1")) as client:
            return [(await fetch(client, creds(GOOD_TOKEN))).status for _ in range(runs)]

    assert set(asyncio.run(main())) == {401}  # a slot not given back would turn the tail into 503
    assert len(spy.calls) == runs  # one registry read per request


def test_a_full_hop_recovers_once_its_reads_finish():
    release = threading.Event()

    class Blocked(SpyRegistry):
        def lookup(self, token: object, fingerprint: object) -> tuple[Device | None, bool]:
            release.wait(10)
            return super().lookup(token, fingerprint)

    spy = Blocked()

    async def main() -> tuple[list[int], int, int]:
        async with TestClient(TestServer(make_app(deps_for(spy), []), host="127.0.0.1")) as client:
            held = [asyncio.create_task(fetch(client, creds(GOOD_TOKEN))) for _ in range(HOP_PENDING_MAX)]
            await asyncio.sleep(0.3)
            refused = (await fetch(client, creds(GOOD_TOKEN))).status
            release.set()
            statuses = [reply.status for reply in await asyncio.gather(*held)]
            return statuses, refused, (await fetch(client, creds(GOOD_TOKEN))).status

    try:
        statuses, refused, after = asyncio.run(main())
    finally:
        release.set()
    assert refused == 503 and set(statuses) == {401} and after == 401


def test_a_refused_hop_is_one_debug_line_per_request_and_names_no_credential(caplog):
    caplog.set_level(logging.DEBUG)
    spy = SpyRegistry()
    deps = AuthDeps(gate_ok_async=always(True), registry=spy, is_revoked=lambda _id: False, hop=RegistryHop(1, 0))
    for _ in range(3):
        assert refusal(get(make_app(deps, []), creds(GOOD_TOKEN)), 503, "unavailable")
    ours = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "glasses.server_auth"]
    assert ours == [(logging.DEBUG, "glasses ws: registry hop full; refusing")] * 3
    assert spy.calls == [] and GOOD_TOKEN not in caplog.text and FP not in caplog.text
