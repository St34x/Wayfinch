# SPDX-License-Identifier: Apache-2.0
"""``EnrollRelay`` executor limits: two running calls fill it and a third is refused, no thread outlives a closed
relay, ``close()`` with a running or queued call, a cancel storm with and without a ``close()`` in the middle, and the
workers' names."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import random
import threading
import time

import enroll_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from enroll_helpers import BODY, app_with, factory, mocked, post, relay_for, send, status_of
from fake_signer import Script

from glasses.enroll import MAX_IN_FLIGHT, EnrollRelay, RateLimiter

fake = enroll_helpers.fake  # the shared fixture, bound here so pytest finds it
PREFIX = "glasses-enroll"


# glasses-enroll threads that other tests left behind (idle pools not yet collected)
EARLIER: set[threading.Thread] = set()  # by object, not ident: an exited thread's ident can be reused


def enroll_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(PREFIX) and t.is_alive() and t not in EARLIER]


def wait_no_enroll_threads(timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not enroll_threads():
            return True
        time.sleep(0.02)
    return not enroll_threads()


async def wait_for_calls(fake: object, count: int) -> None:
    for _ in range(500):
        if len(fake.requests) >= count:  # type: ignore[attr-defined]
            return
        await asyncio.sleep(0.01)
    pytest.fail("the fake signer never saw the calls")


@pytest.fixture(autouse=True)
def _no_enroll_thread_left_behind():
    EARLIER.clear()
    EARLIER.update(t for t in threading.enumerate() if t.name.startswith(PREFIX))
    yield
    assert wait_no_enroll_threads(), "a test left a glasses-enroll thread running"


def test_sequential_requests_never_start_more_than_the_pool_size_of_workers(fake):
    relay = relay_for(fake)
    seen: set[str] = set()
    inner = factory(fake)

    def recording():
        seen.update(t.name for t in enroll_threads())
        return inner()

    relay = EnrollRelay(recording, limiter=RateLimiter(per_minute=1000))
    for _ in range(12):
        assert post(relay).status == 200
    assert 1 <= len(seen) <= MAX_IN_FLIGHT and all(name.startswith(PREFIX) for name in seen)
    relay.close()


def test_a_relay_that_never_served_a_request_has_no_thread_and_closes_cleanly():
    before = len(enroll_threads())
    relays = [EnrollRelay(lambda: pytest.fail("no signer call"), limiter=RateLimiter()) for _ in range(20)]  # type: ignore[arg-type,return-value]
    assert len(enroll_threads()) == before
    for relay in relays:
        relay.close()
        relay.close()
    assert len(enroll_threads()) == before


def test_many_relays_used_and_closed_leave_no_worker_behind(fake):
    for _ in range(15):
        relay = relay_for(fake)
        assert post(relay).status == 200
        relay.close()
    assert wait_no_enroll_threads()


def test_close_with_a_running_call_lets_it_answer_refuses_the_next_and_leaves_no_thread(fake):
    fake.scripts["enrol"] = Script(delay_s=0.6)

    async def main() -> tuple[int, int, int]:
        relay = relay_for(fake)
        async with TestClient(TestServer(app_with(relay), host="127.0.0.1")) as client:
            running = asyncio.create_task(send(client))
            await wait_for_calls(fake, 1)
            relay.close()
            after = await send(client)
            first = await running
        return first.status, after.status, relay._in_flight

    assert asyncio.run(main()) == (200, 503, 0)
    assert len(fake.requests) == 1  # the 503 made no signer call
    assert wait_no_enroll_threads()


def test_close_returns_at_once_while_a_call_is_still_running(fake):
    fake.scripts["enrol"] = Script(delay_s=1.0)

    async def main() -> float:
        relay = relay_for(fake)
        async with TestClient(TestServer(app_with(relay), host="127.0.0.1")) as client:
            running = asyncio.create_task(send(client))
            await wait_for_calls(fake, 1)
            started = time.monotonic()
            relay.close()  # must not wait for the signer: it runs on the gateway loop at shutdown
            took = time.monotonic() - started
            await running
        return took

    assert asyncio.run(main()) < 0.3


def test_a_request_queued_when_close_is_called_makes_no_signer_call_and_holds_no_slot(fake):
    async def main() -> tuple[object, int, int]:
        gate = threading.Event()
        relay = relay_for(fake)
        busy = [relay._executor.submit(gate.wait, 5) for _ in range(MAX_IN_FLIGHT)]
        async with TestClient(TestServer(app_with(relay), host="127.0.0.1")) as client:
            queued = asyncio.create_task(send(client))
            await asyncio.sleep(0.2)
            relay.close()
            try:
                outcome: object = (await asyncio.wait_for(queued, 3)).status
            except Exception as error:  # noqa: BLE001 - the client's view of the dropped request
                outcome = type(error).__name__
            gate.set()
            concurrent.futures.wait(busy)
        await asyncio.sleep(0.1)
        return outcome, len(fake.requests), relay._in_flight

    outcome, calls, in_flight = asyncio.run(main())
    assert (calls, in_flight) == (0, 0)
    assert outcome in (503, "ServerDisconnectedError")  # today the connection is dropped (next test)


@pytest.mark.xfail(
    reason="known gap: a request queued at close() is dropped (CancelledError), not answered 503", strict=True
)
def test_a_request_queued_when_close_is_called_is_answered_503_not_dropped(fake):
    """The client of a queued request should get the same 503 as every later one, not a dropped connection."""

    async def main() -> object:
        gate = threading.Event()
        relay = relay_for(fake)
        busy = [relay._executor.submit(gate.wait, 5) for _ in range(MAX_IN_FLIGHT)]
        async with TestClient(TestServer(app_with(relay), host="127.0.0.1")) as client:
            queued = asyncio.create_task(send(client))
            await asyncio.sleep(0.2)
            relay.close()
            try:
                outcome: object = (await asyncio.wait_for(queued, 3)).status
            except Exception as error:  # noqa: BLE001 - the client's view of the dropped request
                outcome = type(error).__name__
            gate.set()
            concurrent.futures.wait(busy)
        return outcome

    assert asyncio.run(main()) == 503


def test_a_closed_relay_with_both_slots_held_answers_429_as_documented_then_503_once_a_slot_is_free(fake):
    """The documented order is 429 (two running) before 503 (closed): a closed relay still full says 429, then 503."""
    fake.scripts["enrol"] = Script(delay_s=0.6)

    async def main() -> tuple[int, int]:
        relay = relay_for(fake)
        async with TestClient(TestServer(app_with(relay), host="127.0.0.1")) as client:
            held = [asyncio.create_task(send(client)) for _ in range(MAX_IN_FLIGHT)]
            await wait_for_calls(fake, MAX_IN_FLIGHT)
            relay.close()
            while_full = await send(client)
            await asyncio.gather(*held)
            await asyncio.sleep(0.1)  # the slots are released from the worker threads
            when_free = await send(client)
        return while_full.status, when_free.status

    assert asyncio.run(main()) == (429, 503)
    assert len(fake.requests) == MAX_IN_FLIGHT  # neither late request reached the signer


@pytest.mark.xfail(reason="known gap: close() from another thread can race the submit and leak the slot", strict=True)
def test_a_submit_refused_because_the_executor_is_shut_down_frees_the_slot(fake):
    relay = relay_for(fake)

    def refused(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    relay._executor.submit = refused  # type: ignore[method-assign]

    async def main() -> int:
        with contextlib.suppress(RuntimeError):
            await status_of(relay, mocked(body=json.dumps(BODY).encode()))
        return relay._in_flight

    assert asyncio.run(main()) == 0


def test_a_cancelled_request_never_started_makes_no_signer_call_and_the_next_one_succeeds(fake):
    async def main() -> tuple[int, int, int]:
        gate = threading.Event()
        relay = relay_for(fake)
        busy = [relay._executor.submit(gate.wait, 5) for _ in range(MAX_IN_FLIGHT)]
        body = json.dumps(BODY).encode()
        tasks = [asyncio.create_task(status_of(relay, mocked(body=body))) for _ in range(MAX_IN_FLIGHT)]
        await asyncio.sleep(0.1)
        in_flight_while_queued = relay._in_flight
        third = await status_of(relay, mocked(body=body))  # both slots taken by the queued two
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        gate.set()
        concurrent.futures.wait(busy)
        again = await status_of(relay, mocked(body=body))
        relay.close()
        return in_flight_while_queued, third, again

    assert asyncio.run(main()) == (MAX_IN_FLIGHT, 429, 200)
    assert len(fake.requests) == 1


class Slow:
    """A signer client with a short seeded pause (no socket)."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def enrol(self, spki: str, code: str, *, sig: str):  # type: ignore[no-untyped-def]
        from glasses.signer_client import EnrolResult

        time.sleep(self._rng.uniform(0, 0.003))
        return EnrolResult(device_id="d", cert="c", token="t")  # noqa: S106 - a stub value


@pytest.mark.parametrize("seed", [11, 12])
def test_a_cancel_storm_with_a_close_in_the_middle_ends_with_no_slot_and_no_thread(seed):
    rng = random.Random(seed)  # noqa: S311 - a reproducible schedule
    relay = EnrollRelay(lambda: Slow(rng), limiter=RateLimiter(per_minute=10**6))  # type: ignore[arg-type,return-value]
    body = json.dumps(BODY).encode()

    async def main() -> list[object]:
        tasks = []
        for number in range(300):
            tasks.append(asyncio.create_task(status_of(relay, mocked(body=body))))
            await asyncio.sleep(rng.uniform(0, 0.001))
            if rng.random() < 0.5:
                tasks[-1].cancel()
            if number == 150:
                relay.close()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for _ in range(300):
            if relay._in_flight == 0:
                break
            await asyncio.sleep(0.01)
        return results

    results = asyncio.run(main())
    assert relay._in_flight == 0
    stray = [r for r in results if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)]
    assert stray == []
    assert {r for r in results if isinstance(r, int)} <= {200, 429, 503}
    assert wait_no_enroll_threads()


@pytest.mark.parametrize("seed", [21, 22])
def test_a_cancel_storm_against_the_fake_signer_ends_with_no_slot_held_and_a_working_relay(fake, seed):
    rng = random.Random(seed)  # noqa: S311 - a reproducible schedule
    relay = relay_for(fake, limiter=RateLimiter(per_minute=10**6))
    body = json.dumps(BODY).encode()

    async def main() -> tuple[int, int]:
        tasks = []
        for _ in range(120):
            tasks.append(asyncio.create_task(status_of(relay, mocked(body=body))))
            await asyncio.sleep(rng.uniform(0, 0.004))
            if rng.random() < 0.5:
                tasks[-1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for _ in range(500):
            if relay._in_flight == 0:
                break
            await asyncio.sleep(0.01)
        return relay._in_flight, await status_of(relay, mocked(body=body))

    assert asyncio.run(main()) == (0, 200)
    relay.close()
