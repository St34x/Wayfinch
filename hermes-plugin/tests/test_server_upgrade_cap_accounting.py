# SPDX-License-Identifier: Apache-2.0
"""WS server per-certificate upgrade cap (T1.07c, keyed on the verified certificate since T1.184c): no slot leaks over
hundreds of failed or abandoned upgrades, and the cap answers before the 32-deep registry bound (which only several
certificates together can reach)."""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from server_auth_helpers import FP, OTHER_FP, SpyRegistry, creds, new_fingerprints, upgrade_headers
from server_helpers import app_for, serving, wiring

from glasses.connections import UPGRADES_PER_CERT_MAX
from glasses.registry import Device
from glasses.server import _AUTH, _CAP
from glasses.server_auth import HOP_PENDING_MAX, HOP_WORKERS

TOKEN = "S3ntinelTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def run(coro: Awaitable[Any], limit: float = 90) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))  # type: ignore[arg-type]


def request_bytes(headers: dict[str, str]) -> bytes:
    lines = "".join(f"{name}: {value}\r\n" for name, value in {**upgrade_headers(), **headers}.items())
    return f"GET /ws HTTP/1.1\r\nHost: x\r\n{lines}\r\n".encode()


async def hang_up(port: int, headers: dict[str, str], source: str = "127.0.0.1") -> None:
    """Send an upgrade request and close the socket without reading the answer."""
    _reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=(source, 0))
    writer.write(request_bytes(headers))
    await writer.drain()
    writer.close()


class Blocked:
    """A registry whose reads block their worker thread until released; counts the reads that started."""

    def __init__(self) -> None:
        self.release, self.started, self._lock = threading.Event(), 0, threading.Lock()

    def lookup(self, token: Any, fingerprint: Any) -> tuple[Device | None, bool]:
        with self._lock:
            self.started += 1
        self.release.wait(20)
        return None, False


async def settled(condition: Callable[[], bool], what: str, limit: float = 10.0) -> None:
    deadline = time.monotonic() + limit
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for: {what}"
        await asyncio.sleep(0.01)


def test_500_failed_and_abandoned_upgrades_leave_no_slot_taken_and_the_next_one_is_admitted(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    registry = SpyRegistry(delay=0.003)
    counter = itertools.count()
    storm = threading.Event()  # set while the 500 upgrades run, cleared before the last request: only they can fail

    async def flaky_gate() -> bool:
        number = next(counter)
        if storm.is_set() and number % 7 == 0:
            raise RuntimeError("gate failure")
        await asyncio.sleep(0.002 * (number % 3))
        return True

    app = app_for(w, registry=registry, gate_ok_async=flaky_gate)
    cap = app[_CAP]
    seen: dict[str, Any] = {}

    async def failing_upgrade(session: aiohttp.ClientSession, port: int, number: int) -> int:
        headers = [{}, creds(TOKEN), creds(TOKEN, OTHER_FP), {"Authorization": "Bearer !!"}][number % 4]
        async with session.get(f"http://127.0.0.1:{port}/ws", headers={**upgrade_headers(), **headers}) as response:
            return response.status

    async def main() -> None:
        async with serving(app) as port, aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as session:
            statuses: list[int] = []
            storm.set()
            for _ in range(10):  # 50 at once per wave: far above the cap of 8, so the cap answers 503 too
                tasks = [asyncio.create_task(failing_upgrade(session, port, n)) for n in range(50)]
                for number in range(5):  # and hangs up halfway through the gate / registry read
                    await hang_up(port, creds(TOKEN, FP if number % 2 else OTHER_FP))
                statuses += await asyncio.gather(*tasks)
            await settled(lambda: not cap._open and app[_AUTH].hop._pending == 0, "every slot to come back")
            storm.clear()  # the gate answers normally from here: the last request is judged by the registry alone
            seen["statuses"], seen["open"] = statuses, dict(cap._open)
            async with session.get(
                f"http://127.0.0.1:{port}/ws", headers={**upgrade_headers(), **w.headers()}
            ) as response:
                seen["next"] = response.status

    run(main())
    assert len(seen["statuses"]) == 500 and set(seen["statuses"]) <= {401, 503}, set(seen["statuses"])
    assert 503 in seen["statuses"] and 401 in seen["statuses"]
    assert seen["open"] == {}
    assert seen["next"] == 401  # no slot leaked: the next upgrade is judged by the registry, not capped
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text
    assert "Traceback" not in caplog.text


def test_100_clients_that_hang_up_with_valid_credentials_leave_no_slot_and_no_error_record(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    app = app_for(w)
    cap = app[_CAP]

    async def main() -> dict[str, Any]:
        async with serving(app) as port, aiohttp.ClientSession() as session:
            for _ in range(100):
                await hang_up(port, w.headers())  # admitted by the registry, gone before or during the upgrade
            await settled(lambda: not cap._open, "the slots to come back")
            async with session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers()) as ws:
                return {"open": dict(cap._open), "closed": ws.closed}

    seen = run(main())
    assert seen == {"open": {}, "closed": False}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text


def test_one_certificate_never_reaches_the_registry_bound_and_the_cap_is_the_answer_below_it(tmp_path):
    w = wiring(tmp_path)
    registry = Blocked()
    app = app_for(w, registry=registry)
    seen: dict[str, Any] = {}

    async def main() -> None:
        async with serving(app) as port, aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as session:

            async def upgrade() -> int:
                headers = {**upgrade_headers(), **creds(TOKEN)}
                async with session.get(f"http://127.0.0.1:{port}/ws", headers=headers) as response:
                    return response.status

            crowd = [asyncio.create_task(upgrade()) for _ in range(50)]
            await settled(lambda: registry.started == HOP_WORKERS, "the workers to start")
            await asyncio.sleep(0.3)
            seen["pending"] = app[_AUTH].hop._pending
            registry.release.set()
            seen["statuses"] = sorted(await asyncio.gather(*crowd))

    run(main())
    assert UPGRADES_PER_CERT_MAX < HOP_PENDING_MAX  # the premise: the cap is the tighter bound for one certificate
    assert seen["pending"] == UPGRADES_PER_CERT_MAX and registry.started == UPGRADES_PER_CERT_MAX
    assert seen["statuses"] == [401] * UPGRADES_PER_CERT_MAX + [503] * (50 - UPGRADES_PER_CERT_MAX)


def test_several_certificates_together_reach_the_registry_bound_which_then_answers_503_and_gives_every_slot_back(
    tmp_path,
):
    w = wiring(tmp_path)
    registry = Blocked()
    app = app_for(w, registry=registry)
    certificates = new_fingerprints(6)  # 6 x 8 = 48 upgrades in flight, more than the hop's 32
    seen: dict[str, Any] = {}

    async def main() -> None:
        async with serving(app) as port:
            writers = []
            for fingerprint in certificates:
                for _ in range(UPGRADES_PER_CERT_MAX):
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.write(request_bytes(creds(TOKEN, fingerprint)))
                    writers.append((reader, writer))
            await settled(lambda: registry.started == HOP_WORKERS, "the workers to start")
            await asyncio.sleep(0.5)
            seen["pending"] = app[_AUTH].hop._pending
            seen["held"] = sum(app[_CAP]._open.values())
            registry.release.set()
            answers = [(await asyncio.wait_for(reader.read(100), 10)).split(b"\r\n")[0] for reader, _ in writers]
            seen["statuses"] = sorted(answers)
            for _, writer in writers:
                writer.close()
            await settled(lambda: app[_AUTH].hop._pending == 0 and not app[_CAP]._open, "every slot to come back")

    run(main())
    assert seen["pending"] == HOP_PENDING_MAX
    assert seen["held"] == HOP_PENDING_MAX  # the 16 the hop refused gave their slots back at once
    assert registry.started == HOP_PENDING_MAX  # 4 running + 28 queued; the other 16 got 503 without a read
    assert seen["statuses"].count(b"HTTP/1.1 503 Service Unavailable") == 16
    assert seen["statuses"].count(b"HTTP/1.1 401 Unauthorized") == HOP_PENDING_MAX
