# SPDX-License-Identifier: Apache-2.0
"""The ``/enroll`` relay runs the signer call in its own two-thread executor (never the loop's default one):
held enrolments leave ``asyncio.to_thread`` free, the workers carry the ``glasses-enroll`` prefix, ``close()`` stops
new calls with a 503, and the in-flight slots stay exact when about half of many seeded requests are cancelled."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import random
import threading
import time

import enroll_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from enroll_helpers import BODY, CODE, SIG, SPKI, app_with, factory, mocked, post, relay_for, send, status_of
from fake_signer import Script

from glasses.enroll import MAX_IN_FLIGHT, EnrollRelay, RateLimiter
from glasses.signer_client import EnrolResult

fake = enroll_helpers.fake  # the shared fixture, bound here so pytest finds it


async def wait_for_calls(fake: object, count: int) -> None:
    for _ in range(500):
        if len(fake.requests) >= count:  # type: ignore[attr-defined]
            return
        await asyncio.sleep(0.01)
    pytest.fail("the fake signer never saw the held enrolments")


def test_held_enrolments_leave_the_default_executor_free(fake):
    fake.scripts["enrol"] = Script(delay_s=1.5)

    async def main() -> str:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT))
        async with TestClient(TestServer(app_with(relay_for(fake)), host="127.0.0.1")) as client:
            held = [asyncio.create_task(send(client)) for _ in range(MAX_IN_FLIGHT)]
            await wait_for_calls(fake, MAX_IN_FLIGHT)
            probe = await asyncio.wait_for(asyncio.to_thread(lambda: "done"), 0.5)
            await asyncio.gather(*held)
            return probe

    assert asyncio.run(main()) == "done"


def test_the_signer_call_runs_in_a_glasses_enroll_thread(fake):
    names: list[str] = []
    inner = factory(fake)

    def recording() -> object:
        names.append(threading.current_thread().name)
        return inner()

    relay = EnrollRelay(recording, limiter=RateLimiter(per_minute=1000))  # type: ignore[arg-type]
    assert post(relay).status == 200
    assert len(names) == 1 and names[0].startswith("glasses-enroll")


def test_a_request_after_close_is_503_and_the_signer_sees_nothing(fake):
    relay = relay_for(fake)
    relay.close()
    relay.close()  # closing twice is harmless
    reply = post(relay)
    assert reply.status == 503
    assert fake.requests == []
    assert relay._in_flight == 0


def test_close_while_requests_are_queued_frees_their_slots(fake):
    async def main() -> tuple[int, int]:
        gate = threading.Event()
        relay = relay_for(fake)
        busy = [relay._executor.submit(gate.wait, 5) for _ in range(MAX_IN_FLIGHT)]
        body = json.dumps(BODY).encode()
        tasks = [asyncio.create_task(status_of(relay, mocked(body=body))) for _ in range(MAX_IN_FLIGHT)]
        await asyncio.sleep(0.1)
        relay.close()  # the queued calls are cancelled and never made
        await asyncio.gather(*tasks, return_exceptions=True)
        gate.set()
        concurrent.futures.wait(busy)
        await asyncio.sleep(0.1)
        return len(fake.requests), relay._in_flight

    assert asyncio.run(main()) == (0, 0)


class Stub:
    """A signer client that answers after a short seeded pause (no socket)."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def enrol(self, spki: str, code: str, *, sig: str) -> EnrolResult:
        assert (spki, code, sig) == (SPKI, CODE, SIG)
        time.sleep(self._rng.uniform(0, 0.004))
        return EnrolResult(device_id="d", cert="c", token="t")  # noqa: S106 - a stub value, not a secret


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_about_half_cancelled_requests_end_with_no_slot_held(seed):
    rng = random.Random(seed)  # noqa: S311 - a reproducible schedule, not a secret
    relay = EnrollRelay(lambda: Stub(rng), limiter=RateLimiter(per_minute=10**6))  # type: ignore[arg-type]
    body = json.dumps(BODY).encode()

    async def main() -> list[object]:
        tasks = []
        for _ in range(400):
            task = asyncio.create_task(status_of(relay, mocked(body=body)))
            tasks.append(task)
            await asyncio.sleep(rng.uniform(0, 0.002))
            if rng.random() < 0.5:
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for _ in range(200):
            if relay._in_flight == 0:
                break
            await asyncio.sleep(0.01)
        return results

    results = asyncio.run(main())
    assert relay._in_flight == 0
    assert {r for r in results if isinstance(r, int)} <= {200, 429}
    relay.close()
