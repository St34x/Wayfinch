# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a), guards: a registry stand-in answering the wrong type is the fixed 401 without
a traceback, and a failure that repeats is one WARNING line (the operator's signal), not one per request."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from aiohttp.test_utils import TestClient, TestServer
from server_auth_helpers import FP, SpyRegistry, always, creds, deps_for, enrolled, fetch, get, make_app, refusal

from glasses.server_auth import AuthDeps, HopFull, RegistryHop

UNAUTHORIZED = (401, "unauthorized")
DETAIL = "detail-that-must-stay-out-of-every-log-line"
TOKEN = "t" * 43
RETRIES = 50


class Tripwire:
    """A registry answer of the wrong type: any attribute read or use of it would raise or leak its text."""

    revoked_at = None

    def __repr__(self) -> str:
        return DETAIL

    def __str__(self) -> str:
        return DETAIL


def ours(caplog: Any) -> list[tuple[int, str]]:
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "glasses.server_auth"]


def test_a_registry_answering_a_non_device_is_the_fixed_401_without_a_traceback(caplog):
    caplog.set_level(logging.DEBUG)
    spy = SpyRegistry(device=Tripwire())  # type: ignore[arg-type]
    handled: list[str] = []
    reply = get(make_app(deps_for(spy), handled), creds(TOKEN))
    assert refusal(reply, *UNAUTHORIZED), reply
    assert handled == []
    assert "Traceback" not in caplog.text and DETAIL not in caplog.text and TOKEN not in caplog.text
    assert "TypeError" in caplog.text  # the type only


def test_a_registry_answering_a_non_boolean_tombstone_is_the_fixed_401_not_the_wipe_marker(caplog):
    caplog.set_level(logging.DEBUG)
    spy = SpyRegistry(tombstone=Tripwire())  # type: ignore[arg-type]
    reply = get(make_app(deps_for(spy), []), creds(TOKEN))
    assert refusal(reply, *UNAUTHORIZED), reply
    assert DETAIL not in caplog.text and "Traceback" not in caplog.text


def test_a_registry_that_fails_on_every_retry_is_one_warning_line_and_warns_again_after_a_good_read(caplog):
    caplog.set_level(logging.DEBUG)
    spy = SpyRegistry(error=ValueError(DETAIL))

    async def main() -> list[int]:
        statuses = []
        async with TestClient(TestServer(make_app(deps_for(spy), []), host="127.0.0.1")) as client:
            for _ in range(RETRIES):
                statuses.append((await fetch(client, creds(TOKEN))).status)
            spy.error = None  # the registry recovers: a wrong token is the plain 401 again
            statuses.append((await fetch(client, creds(TOKEN))).status)
            spy.error = ValueError(DETAIL)  # and a new failure is news again
            statuses.append((await fetch(client, creds(TOKEN))).status)
        return statuses

    assert set(asyncio.run(main())) == {401}
    warnings = [message for level, message in ours(caplog) if level == logging.WARNING]
    assert warnings == ["glasses ws: registry error (ValueError); refusing"] * 2
    assert len([1 for level, _m in ours(caplog) if level == logging.DEBUG]) == RETRIES - 1
    assert DETAIL not in caplog.text and TOKEN not in caplog.text and FP not in caplog.text


def test_a_gate_that_fails_on_every_retry_is_one_warning_line(caplog):
    caplog.set_level(logging.DEBUG)

    async def broken() -> bool:
        raise RuntimeError(DETAIL)

    spy = SpyRegistry()
    deps = AuthDeps(gate_ok_async=broken, registry=spy, is_revoked=lambda _id: False)

    async def main() -> list[int]:
        async with TestClient(TestServer(make_app(deps, []), host="127.0.0.1")) as client:
            return [(await fetch(client, creds(TOKEN))).status for _ in range(RETRIES)]

    assert set(asyncio.run(main())) == {503}
    warnings = [message for level, message in ours(caplog) if level == logging.WARNING]
    assert warnings == ["glasses ws: allow-all gate check failed (RuntimeError)"]
    assert spy.calls == [] and DETAIL not in caplog.text


def test_a_revocation_check_that_keeps_failing_is_one_warning_line(tmp_path, caplog):
    registry, _device, token = enrolled(tmp_path)
    caplog.set_level(logging.DEBUG)

    def broken(device_id: str) -> bool:
        raise RuntimeError(DETAIL)

    deps = AuthDeps(gate_ok_async=always(True), registry=registry, is_revoked=broken)

    async def main() -> list[int]:
        async with TestClient(TestServer(make_app(deps, []), host="127.0.0.1")) as client:
            return [(await fetch(client, creds(token))).status for _ in range(RETRIES)]

    assert set(asyncio.run(main())) == {503}
    warnings = [message for level, message in ours(caplog) if level == logging.WARNING]
    assert warnings == ["glasses ws: revocation check failed (RuntimeError); refusing"]
    assert DETAIL not in caplog.text and token not in caplog.text


def hop_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name.startswith("glasses-auth")}


def test_close_stops_the_hops_threads_and_a_later_run_is_refused_not_hung():
    before = hop_threads()
    hop = RegistryHop(workers=2, pending_max=4)

    async def main() -> None:
        assert await hop.run(lambda: 1) == 1
        assert hop_threads() - before  # the pool started threads of its own
        hop.close()
        try:
            await asyncio.wait_for(hop.run(lambda: 2), 2)
        except HopFull:
            return
        raise AssertionError("a closed hop ran work")

    asyncio.run(main())
    deadline = time.monotonic() + 5
    while hop_threads() - before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hop_threads() - before == set()
    assert hop._pending == 0


def test_close_drops_the_queued_items_and_gives_their_slots_back():
    release = threading.Event()
    hop = RegistryHop(workers=1, pending_max=5)

    async def main() -> None:
        tasks = [asyncio.create_task(hop.run(release.wait, 10)) for _ in range(5)]
        await asyncio.sleep(0.1)
        hop.close()  # 1 running, 4 queued
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        release.set()
        while hop._pending:
            await asyncio.sleep(0.01)

    try:
        asyncio.run(asyncio.wait_for(main(), 5))
    finally:
        release.set()
    assert hop._pending == 0
