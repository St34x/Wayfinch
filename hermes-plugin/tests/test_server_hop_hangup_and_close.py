# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), the 32-deep registry bound over a real aiohttp server run by ``build_runner``: 300 clients
that hang up against blocked workers, with ``handler_cancellation`` off (production) and on (what ``TestServer``
forces), keep the bound while they are gone and give every slot back once the workers are free; and
``RegistryHop.close()`` racing with running, queued and newly submitted work."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import web
from server_auth_helpers import FP, cert_header, upgrade_headers
from server_helpers import app_for, raw, serving, wiring

from glasses.connections import UpgradeCap
from glasses.server import _AUTH, _CAP, build_runner
from glasses.server_auth import HOP_PENDING_MAX, HOP_WORKERS, HopFull, RegistryHop


def hop_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name.startswith("glasses-auth")}


async def until(condition: Callable[[], bool], what: str, limit: float = 10.0) -> None:
    deadline = time.monotonic() + limit
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for: {what}"
        await asyncio.sleep(0.01)


class BlockedRegistry:
    """``lookup`` blocks its worker thread until released and counts the reads that started."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = 0
        self._lock = threading.Lock()

    def lookup(self, token: Any, fingerprint: Any) -> tuple[None, bool]:
        with self._lock:
            self.started += 1
        self.release.wait(20)
        return None, False


def upgrade_request(token: str, fingerprint: str) -> bytes:
    headers = {**upgrade_headers(), "Authorization": f"Bearer {token}", **cert_header(fingerprint)}
    lines = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return f"GET /ws HTTP/1.1\r\nHost: x\r\n{lines}\r\n".encode()


@pytest.mark.parametrize("cancellation", [False, True], ids=["handler_cancellation_off", "handler_cancellation_on"])
def test_300_hang_ups_against_blocked_workers_keep_the_bound_and_return_every_slot(tmp_path, caplog, cancellation):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    registry = BlockedRegistry()
    app = app_for(w, registry=registry)
    hop: RegistryHop = app[_AUTH].hop
    app[_CAP] = UpgradeCap(
        per_key=400
    )  # the bound under test is the hop's; the per-certificate cap (8) would answer first
    request = upgrade_request("t" * 43, FP)
    seen: dict[str, Any] = {}

    async def main() -> None:
        runner = build_runner(app)
        runner._kwargs["handler_cancellation"] = cancellation  # the production runner pins it off; test both modes
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = int(runner.addresses[0][1])
        try:
            writers = []
            for _ in range(300):
                _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(request)
                writers.append(writer)
            await until(lambda: registry.started == HOP_WORKERS, "the workers to start their reads")
            await asyncio.sleep(0.5)
            seen["before_hang_up"] = hop._pending
            for writer in writers:
                writer.close()
            await asyncio.sleep(0.5)
            seen["after_hang_up"] = hop._pending
            seen["probe"] = await raw(port, request)  # a new client while the gone ones still hold the slots
            registry.release.set()
            await until(lambda: hop._pending == 0, "every slot to come back")
            seen["reads"] = registry.started
            seen["next"] = await raw(port, request)
        finally:
            registry.release.set()
            await runner.cleanup()

    asyncio.run(asyncio.wait_for(main(), 60))
    assert seen["before_hang_up"] == HOP_PENDING_MAX
    assert seen["after_hang_up"] == HOP_PENDING_MAX  # a hang-up frees nothing while the item waits in the queue
    assert seen["probe"].startswith(b"HTTP/1.1 503 ") and b'{"error":"unavailable"}' in seen["probe"]
    assert seen["reads"] == HOP_PENDING_MAX  # 4 running + 28 queued; the other 268 were refused without a read
    assert seen["next"].startswith(b"HTTP/1.1 401 ")  # the bound is open again (the blocked reads were released)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text
    assert "never retrieved" not in caplog.text and "Traceback" not in caplog.text
    deadline = time.monotonic() + 5
    while hop_threads() and time.monotonic() < deadline:
        time.sleep(0.01)


def blocking(release: threading.Event, running: list[int]) -> Callable[[int], int]:
    def call(number: int) -> int:
        running.append(number)
        release.wait(20)
        return number

    return call


def test_close_with_running_and_queued_work_refuses_new_runs_answers_every_waiter_at_once_and_returns_every_slot():
    release, running = threading.Event(), []

    async def main() -> tuple[list[Any], BaseException | None, int, int]:
        hop = RegistryHop(workers=2, pending_max=8)
        try:
            tasks = [asyncio.create_task(hop.run(blocking(release, running), n)) for n in range(8)]
            await until(lambda: len(running) == 2, "two reads to be running")
            hop.close()
            late: BaseException | None = None
            try:
                await hop.run(blocking(release, running), 99)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                late = exc
            # the running reads are NOT released: their waiters must be answered anyway (the worker is not joined)
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
            held = hop._pending  # the two running reads still hold their workers' slots
            release.set()
            await until(lambda: hop._pending == 0, "every slot to come back")
            return results, late, held, hop._pending
        finally:
            release.set()

    results, late, held, pending = asyncio.run(main())
    assert isinstance(late, HopFull) and (held, pending) == (2, 0)
    assert all(isinstance(r, HopFull) for r in results) and len(results) == 8  # not CancelledError: a 503 each
    assert len(running) == 2  # a queued item never ran


def test_close_called_from_another_thread_while_runs_are_submitted_never_leaks_a_slot(caplog):
    caplog.set_level(logging.DEBUG)
    outcomes: set[str] = set()

    async def one_race(seed: int) -> None:
        rng = random.Random(seed)  # noqa: S311 - timing jitter for a race, not security
        hop = RegistryHop(workers=2, pending_max=6)

        def work(n: int) -> int:
            time.sleep(rng.random() * 0.002)
            return n

        closer = threading.Timer(rng.random() * 0.004, hop.close)
        closer.start()
        tasks = []
        for n in range(20):
            tasks.append(asyncio.create_task(hop.run(work, n)))
            await asyncio.sleep(rng.random() * 0.0005)
        for result in await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10):
            outcomes.add("ok" if isinstance(result, int) else type(result).__name__)
        closer.join()
        await until(lambda: hop._pending == 0, f"slots to come back (race {seed})", 5)

    async def main() -> None:
        for seed in range(60):
            await one_race(seed)

    asyncio.run(main())
    assert outcomes <= {"ok", "HopFull"}, outcomes  # a submit that loses the race with close() is HopFull too (a 503)
    assert "never retrieved" not in caplog.text and "Traceback" not in caplog.text


def test_a_submit_that_loses_the_race_with_close_is_hop_full_and_gives_its_slot_back(monkeypatch):
    hop = RegistryHop(workers=1, pending_max=2)

    def refused(*_args: Any) -> Any:
        raise RuntimeError("cannot schedule new futures after shutdown")  # what the pool says after close()

    monkeypatch.setattr(hop._pool, "submit", refused)

    async def main() -> None:
        with pytest.raises(HopFull):
            await hop.run(lambda: 1)
        assert hop._pending == 0

    asyncio.run(main())


def test_an_upgrade_that_loses_the_race_with_close_is_503_not_401(tmp_path, monkeypatch):
    w = wiring(tmp_path)
    app = app_for(w)
    hop = app[_AUTH].hop

    def refused(*_args: Any) -> Any:
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(hop._pool, "submit", refused)

    async def main() -> bytes:
        async with serving(app) as port:
            return await raw(port, upgrade_request(w.token, FP))

    answer = asyncio.run(asyncio.wait_for(main(), 20))
    assert answer.startswith(b"HTTP/1.1 503 "), answer[:60]


def test_a_client_whose_queued_read_is_dropped_by_close_still_gets_an_http_answer(tmp_path):
    w = wiring(tmp_path)
    registry = BlockedRegistry()
    app = app_for(w, registry=registry)
    request = upgrade_request("t" * 43, FP)

    async def answer(port: int) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        try:
            return await asyncio.wait_for(reader.read(100), 10)
        finally:
            writer.close()

    async def main() -> list[bytes]:
        runner = build_runner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = int(runner.addresses[0][1])
        try:
            clients = [asyncio.create_task(answer(port)) for _ in range(HOP_WORKERS + 2)]
            await until(lambda: registry.started == HOP_WORKERS, "the workers to start their reads")
            await until(lambda: app[_AUTH].hop._pending == HOP_WORKERS + 2, "two more reads to be queued")
            app[_AUTH].hop.close()  # drops the two queued reads
            registry.release.set()
            return list(await asyncio.gather(*clients))
        finally:
            registry.release.set()
            await runner.cleanup()

    answers = asyncio.run(asyncio.wait_for(main(), 40))
    assert all(a.startswith(b"HTTP/1.1 ") for a in answers), answers
