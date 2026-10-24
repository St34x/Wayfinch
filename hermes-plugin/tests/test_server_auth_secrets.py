# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a): no secret in any output, no blocking registry I/O on the event loop."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import traceback

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from server_auth_helpers import CERT_VALUES, FP, OTHER_FP, SpyRegistry, creds, deps_for, enrolled, fetch, get, make_app

from glasses.registry import Device
from glasses.registry_parse import RegistryCorruptError
from glasses.server_auth import authenticate

TOKEN = "sentinel-token-0123456789_abcdefghijklmnopqrs"
FINGERPRINT = FP  # the verified fingerprint of the test certificate, and the certificate bytes as nginx sends them
SECRETS = (TOKEN, FINGERPRINT, hashlib.sha256(TOKEN.encode()).hexdigest(), CERT_VALUES[FP][90:150])


def leaks(text: str) -> list[str]:
    return [s for s in SECRETS if s in text]


def test_a_planted_token_and_fingerprint_appear_in_no_response_and_no_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    registry, device, _issued = enrolled(tmp_path, FINGERPRINT)
    hidden = RegistryCorruptError(f"entry {TOKEN} {FINGERPRINT}")
    kept = Device(device.device_id, "n", FINGERPRINT, (), (), "t", None, "2026-10-17T00:00:00Z")
    active = Device(device.device_id, "n", FINGERPRINT, (), (), "t", None, None)
    cases = [
        (deps_for(registry), creds(TOKEN, FINGERPRINT)),  # unknown token
        (deps_for(registry), creds(TOKEN, OTHER_FP)),  # fingerprint mismatch
        (deps_for(registry, gate=False), creds(TOKEN, FINGERPRINT)),  # 503
        (deps_for(SpyRegistry(error=hidden)), creds(TOKEN, FINGERPRINT)),  # registry error with the values in its text
        (deps_for(SpyRegistry(error=OSError(TOKEN))), creds(TOKEN, FINGERPRINT)),
        (deps_for(SpyRegistry(device=kept)), creds(TOKEN, FINGERPRINT)),  # 403
        (deps_for(SpyRegistry(tombstone=True)), creds(TOKEN, FINGERPRINT)),  # 403
        (deps_for(SpyRegistry(device=active), revoked=frozenset({device.device_id})), creds(TOKEN, FINGERPRINT)),
    ]
    statuses = []
    for deps, headers in cases:
        reply = get(make_app(deps, []), headers)
        statuses.append(reply.status)
        assert leaks(reply.body.decode() + str(dict(reply.headers))) == []
    assert statuses == [401, 401, 503, 401, 401, 403, 403, 403]
    assert leaks(caplog.text) == []
    assert leaks(" ".join(str(r.args) + r.getMessage() + str(r.exc_info) for r in caplog.records)) == []


def test_the_exceptions_authenticate_raises_carry_no_value_and_no_chained_cause(tmp_path):
    hidden = RegistryCorruptError(f"entry {TOKEN} {FINGERPRINT}")

    async def raised(deps) -> BaseException:
        request = make_mocked_request("GET", "/ws", headers=creds(TOKEN, FINGERPRINT))
        try:
            await authenticate(request, deps, FINGERPRINT)
        except BaseException as exc:  # noqa: BLE001 - the test inspects whatever was raised
            return exc
        raise AssertionError("authenticate returned a device")

    for deps in (deps_for(SpyRegistry(error=hidden)), deps_for(SpyRegistry(), gate=False), deps_for(SpyRegistry())):
        exc = asyncio.run(raised(deps))
        text = "".join(traceback.format_exception(exc)) + repr(exc) + str(exc) + str(exc.__dict__)
        assert leaks(text) == [], text
        assert exc.__cause__ is None
        assert exc.__context__ is None or leaks(str(exc.__context__)) == []


def test_a_slow_registry_read_does_not_stall_the_loop(tmp_path):
    ticked, go = threading.Event(), threading.Event()  # five loop ticks seen / the test is done probing
    reached: list[bool] = []

    class Slow(SpyRegistry):
        def _wait(self) -> None:  # the blocking read, in the worker thread: it ends when the test says so
            reached.append(ticked.wait(5) and go.wait(5))

    async def ticker(stop: asyncio.Event, count: list[int]) -> None:
        while not stop.is_set():
            count[0] += 1
            if count[0] == 5:
                ticked.set()
            await asyncio.sleep(0.01)

    async def main() -> tuple[int, str]:
        async with TestClient(TestServer(make_app(deps_for(Slow()), []), host="127.0.0.1")) as client:
            stop, count = asyncio.Event(), [0]
            beat = asyncio.create_task(ticker(stop, count))
            slow_one = asyncio.create_task(fetch(client, creds("t" * 43)))
            await asyncio.wait_for(asyncio.to_thread(ticked.wait, 5), 6)  # five loop ticks while the read is blocked
            quick = await fetch(client, {}, "/ping")
            still_running = not slow_one.done()
            go.set()
            await slow_one
            stop.set()
            await beat
            return count[0], f"{quick.status}:{quick.body.decode()}:{still_running}"

    ticks, quick = asyncio.run(main())
    assert reached == [True]  # the read waited for the loop's ticks (a read on the loop thread never gets them)
    assert quick == "200:pong:True"  # a concurrent request was answered while the slow read was still running
    assert ticks >= 5


def test_the_registry_is_read_off_the_loop_thread(tmp_path):
    seen: list[int] = []

    class Where(SpyRegistry):
        def lookup(self, token, fingerprint):
            seen.append(threading.get_ident())
            return super().lookup(token, fingerprint)

    async def main() -> int:
        async with TestClient(TestServer(make_app(deps_for(Where()), []), host="127.0.0.1")) as client:
            await fetch(client, creds("t" * 43))
            return threading.get_ident()

    loop_thread = asyncio.run(main())
    assert len(seen) == 1
    assert seen[0] != loop_thread
