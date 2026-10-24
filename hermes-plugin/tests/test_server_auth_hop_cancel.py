# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a): the 32-deep bound of the registry hop holds when clients hang up.

``aiohttp.test_utils.TestServer`` runs every handler with ``handler_cancellation=True`` (the production runner does
not): a hang-up cancels the awaiting handler, and a cancelled work item must still count until a worker has finished it.
"""

from __future__ import annotations

import asyncio
import threading
import time

from aiohttp.test_utils import TestClient, TestServer
from server_auth_helpers import FP, SpyRegistry, creds, deps_for, make_app, upgrade_headers

from glasses.registry import Device
from glasses.server_auth import HOP_PENDING_MAX, HOP_WORKERS, HopFull, RegistryHop

from test_server_hop_hangup_and_close import until

GOOD_TOKEN = "t" * 43


def test_the_hops_constants_match_the_documented_pool():
    assert (HOP_WORKERS, HOP_PENDING_MAX) == (4, 32)  # hermes-plugin/glasses/README.md names both


def test_cancelled_runs_against_blocked_workers_keep_their_slots_until_a_worker_finishes_them():
    release = threading.Event()
    hop = RegistryHop()
    runs = 3 * HOP_PENDING_MAX

    async def blocked() -> None:
        await hop.run(release.wait, 10)

    async def main() -> tuple[int, int, bool]:
        tasks = [asyncio.create_task(blocked()) for _ in range(runs)]
        await asyncio.sleep(0.3)
        refused_before = sum(1 for task in tasks if task.done())  # beyond the bound: HopFull at once
        for task in tasks:
            task.cancel()  # every client hangs up
        await asyncio.gather(*tasks, return_exceptions=True)
        held = hop._pending
        try:
            await hop.run(lambda: None)
        except HopFull:
            return refused_before, held, True
        return refused_before, held, False

    try:
        refused_before, held, full = asyncio.run(main())
    finally:
        release.set()
    assert refused_before == runs - HOP_PENDING_MAX
    assert held == HOP_PENDING_MAX and full, "hang-ups must not free the slots of work still queued"
    deadline = time.monotonic() + 5
    while hop._pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hop._pending == 0  # the workers drained the queue: the bound is released


def test_a_hop_that_drained_its_cancelled_work_runs_again():
    release = threading.Event()
    hop = RegistryHop(workers=1, pending_max=2)

    async def main() -> int:
        tasks = [asyncio.create_task(hop.run(release.wait, 10)) for _ in range(2)]
        await asyncio.sleep(0.1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        release.set()
        while hop._pending:
            await asyncio.sleep(0.01)
        return await hop.run(lambda: 7)

    try:
        assert asyncio.run(asyncio.wait_for(main(), 5)) == 7
    finally:
        release.set()


def test_a_failing_cancelled_read_leaves_no_unretrieved_exception(recwarn, caplog):
    hop = RegistryHop(workers=1, pending_max=2)
    started = threading.Event()
    release = threading.Event()

    def failing() -> None:
        started.set()
        release.wait(10)
        raise OSError("read failed")

    async def main() -> None:
        task = asyncio.create_task(hop.run(failing))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        while hop._pending:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    try:
        asyncio.run(asyncio.wait_for(main(), 5))
    finally:
        release.set()
    assert "never retrieved" not in caplog.text and list(recwarn) == []


class Hanging(SpyRegistry):
    """lookup() blocks until released, as a read on a slow disk does; ``entered`` records each read that started."""

    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self.release = release
        self.entered: list[str] = []

    def lookup(self, token: object, fingerprint: object) -> tuple[Device | None, bool]:
        self.entered.append("lookup")  # runs in a worker thread: list.append is atomic
        self.release.wait(10)
        return super().lookup(token, fingerprint)


def test_hanging_up_clients_through_a_cancelling_test_server_cannot_queue_more_than_the_bound():
    hangups = HOP_PENDING_MAX + 20
    release = threading.Event()
    spy = Hanging(release)
    deps = deps_for(spy)

    async def main() -> tuple[int, int]:  # (slots held, work items waiting in the pool queue)
        async with TestClient(TestServer(make_app(deps, []), host="127.0.0.1")) as client:
            headers = {**upgrade_headers(), **creds(GOOD_TOKEN, FP)}
            requests = [asyncio.ensure_future(client.get("/ws", headers=headers)) for _ in range(hangups)]
            # Hang up only when the hop is full and every worker is inside the registry: no timing involved.
            await until(lambda: len(spy.entered) == HOP_WORKERS and deps.hop._pending == HOP_PENDING_MAX, "a full hop")
            answered = [r for r in requests if r.done()]  # the 503s of the requests the full hop refused
            for request in requests:
                request.cancel()  # the client closes the connection: the server cancels the handler
            await asyncio.gather(*requests, return_exceptions=True)
            handler = client.server.handler
            await until(lambda: len(handler.connections) <= len(answered), "the server to see the hang-ups")
            await asyncio.sleep(0.1)  # the cancelled handlers run their cleanup
            return deps.hop._pending, deps.hop._pool._work_queue.qsize()

    try:
        pending, queued = asyncio.run(asyncio.wait_for(main(), 20))
    finally:
        release.set()
    assert HOP_WORKERS <= pending <= HOP_PENDING_MAX  # hung-up requests still hold their slots
    assert queued <= HOP_PENDING_MAX - HOP_WORKERS  # no dead item beyond the bound waits in the queue
