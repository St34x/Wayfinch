# SPDX-License-Identifier: Apache-2.0
"""``Retry-After: 5`` on the ``503`` of ``/ws`` (T1.184b): one test per source (upgrade cap, closed hop, closed
gate, raising revocation check), each reads the header, and a refusal that is not a ``503`` carries none."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
from server_auth_helpers import SpyRegistry, always, creds, deps_for, enrolled, get, make_app, refusal, upgrade_headers
from server_helpers import app_for, serving, wiring

from glasses.connections import UPGRADES_PER_CERT_MAX
from glasses.server_auth import AuthDeps

TOKEN = "S3ntinelTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def test_the_503_of_the_upgrade_cap_carries_retry_after_5(tmp_path: Path) -> None:
    w = wiring(tmp_path)
    app = app_for(w, registry=SpyRegistry(delay=0.5))

    async def main() -> list[tuple[int, str | None]]:
        async with serving(app) as port, aiohttp.ClientSession() as session:

            async def upgrade() -> tuple[int, str | None]:
                headers = {**upgrade_headers(), **creds(TOKEN)}
                async with session.get(f"http://127.0.0.1:{port}/ws", headers=headers) as response:
                    return response.status, response.headers.get("Retry-After")

            return await asyncio.gather(*(upgrade() for _ in range(UPGRADES_PER_CERT_MAX + 1)))

    replies = asyncio.run(asyncio.wait_for(main(), 30))
    assert sorted(replies) == [(401, None)] * UPGRADES_PER_CERT_MAX + [(503, "5")]


def test_the_503_of_a_closed_registry_hop_carries_retry_after_5() -> None:
    deps = deps_for(SpyRegistry())
    deps.hop.close()  # every later read is refused with 503 without a registry call
    reply = get(make_app(deps, []), creds(TOKEN))
    assert refusal(reply, 503, "unavailable") and reply.headers["Retry-After"] == "5"


def test_the_503_of_a_closed_gate_carries_retry_after_5() -> None:
    reply = get(make_app(deps_for(SpyRegistry(), gate=False), []), creds(TOKEN))
    assert refusal(reply, 503, "unavailable") and reply.headers["Retry-After"] == "5"


def test_the_503_of_a_raising_revocation_check_carries_retry_after_5(tmp_path: Path) -> None:
    registry, _device, token = enrolled(tmp_path)

    def broken(_device_id: str) -> bool:
        raise RuntimeError("the check failed")

    deps = AuthDeps(gate_ok_async=always(True), registry=registry, is_revoked=broken)
    reply = get(make_app(deps, []), creds(token))
    assert refusal(reply, 503, "unavailable") and reply.headers["Retry-After"] == "5"


def test_a_401_carries_no_retry_after() -> None:
    reply = get(make_app(deps_for(SpyRegistry()), []), creds(TOKEN))
    assert refusal(reply, 401, "unauthorized") and "Retry-After" not in reply.headers
