# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a), floods: a malformed credential costs no registry read and no thread hop, and
a flood of well-formed wrong upgrades neither starves the loop's default executor nor queues without bound."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from server_auth_helpers import FP, SpyRegistry, creds, deps_for, fetch, get, make_app, refusal

from glasses.server_auth import HopFull, RegistryHop, authenticate

UNAUTHORIZED = (401, "unauthorized")
UNAVAILABLE = (503, "unavailable")
GOOD_TOKEN = "t" * 43


@pytest.mark.parametrize("token", ["", "a b", "t" * 257, "tok.en", "tok/en", "tok=en", "t" * 43 + "!"])
def test_a_malformed_token_is_401_without_a_registry_read(token):
    spy = SpyRegistry()
    assert refusal(get(make_app(deps_for(spy), []), creds(token)), *UNAUTHORIZED)
    assert spy.calls == []


def _authenticate(spy: SpyRegistry, token: str, fingerprint: str) -> int:
    """``authenticate`` called as the server calls it, with a fingerprint as ``edge_cert`` would hand it over."""

    async def main() -> int:
        request = make_mocked_request("GET", "/ws", headers={"Authorization": f"Bearer {token}"})
        try:
            await authenticate(request, deps_for(spy), fingerprint)
        except web.HTTPException as refused:
            return refused.status
        return 101

    return asyncio.run(main())


@pytest.mark.parametrize("fingerprint", ["", "ab" * 31, "ab" * 33, "zz" * 32, "ab" * 31 + "a-"])
def test_a_malformed_fingerprint_is_401_without_a_registry_read(fingerprint):
    spy = SpyRegistry()
    assert _authenticate(spy, GOOD_TOKEN, fingerprint) == 401
    assert spy.calls == []


@pytest.mark.parametrize("fingerprint", [FP, FP.upper()])
def test_a_well_formed_wrong_credential_still_reaches_the_registry(fingerprint):
    spy = SpyRegistry()
    assert _authenticate(spy, GOOD_TOKEN, fingerprint) == 401
    assert spy.calls == ["lookup"]


def test_a_flood_of_wrong_upgrades_does_not_stall_the_default_executor_and_is_bounded():
    spy = SpyRegistry(delay=0.05)  # a slow registry read (network home, busy disk)
    flood = 200

    async def main() -> tuple[float, list[int]]:
        async with TestClient(TestServer(make_app(deps_for(spy), []), host="127.0.0.1")) as client:
            tasks = [asyncio.create_task(fetch(client, creds(GOOD_TOKEN))) for _ in range(flood)]
            await asyncio.sleep(0.15)  # the flood is in the registry hop
            started = time.monotonic()
            await asyncio.to_thread(lambda: None)  # an unrelated caller of the default executor
            waited = time.monotonic() - started
            return waited, [reply.status for reply in await asyncio.gather(*tasks)]

    waited, statuses = asyncio.run(main())
    assert waited < 0.5, waited
    assert set(statuses) == {401, 503}, set(statuses)
    assert statuses.count(503) > 0
    assert len(spy.calls) < 2 * flood  # the refused ones read nothing


def test_the_hop_refuses_work_beyond_its_pending_bound_and_runs_the_rest():
    release = threading.Event()
    hop = RegistryHop(workers=1, pending_max=2)

    def work() -> str:
        release.wait(10)
        return "done"

    async def main() -> tuple[list[object], bool]:
        first = asyncio.create_task(hop.run(work))
        second = asyncio.create_task(hop.run(work))
        await asyncio.sleep(0.05)
        try:
            await hop.run(work)
            refused = False
        except HopFull:
            refused = True
        release.set()
        return list(await asyncio.gather(first, second)), refused

    results, refused = asyncio.run(main())
    assert results == ["done", "done"] and refused
