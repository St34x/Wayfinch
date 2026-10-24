# SPDX-License-Identifier: Apache-2.0
"""Edges of the inbound dedupe table and the lane checks (T1.08), with recording ports: the table keeps exactly the
newest 1024 ids per device however long it runs, a duplicate neither renews nor evicts anything, identical frames that
race are handed on once, and a failed or cancelled hand-on is released so the phone's re-send is processed. Frames
are built here without the schema check of ``test_inbound.msg`` (thousands of them; lanes the schema refuses too)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import pytest

from glasses import inbound

from test_inbound import DEVICE, Recorder, build, settings, ulid

WORK = ("assistant", "work")


def frame(frame_id: str = ulid(0), lane: str | None = "work") -> Any:
    built: dict[str, Any] = {"v": 1, "id": frame_id, "type": "msg.in", "ts": "2026-10-21T00:00:00Z"}
    built["body"] = {"text": "t", "origin": "typed"}
    if lane is not None:
        built["lane"] = lane
    return built


def route_ids(router: inbound.Inbound, ids: Iterable[str], device: str = DEVICE) -> list[str]:
    """The outcomes of *ids* routed in order in one event loop."""

    async def go() -> list[str]:
        return [await router.msg_in(device, WORK, frame(i)) for i in ids]

    return asyncio.run(go())


def test_exactly_1024_ids_are_all_held_and_the_1025th_drops_the_oldest_only():
    router, rec, _ = build()
    held = [ulid(n) for n in range(inbound.DEDUPE_MAX_IDS)]
    route_ids(router, held)
    assert route_ids(router, held) == [inbound.DUPLICATE] * inbound.DEDUPE_MAX_IDS
    assert len(rec.delivered) == inbound.DEDUPE_MAX_IDS
    assert route_ids(router, [ulid(5000), ulid(1), ulid(0)]) == [
        inbound.HANDED_ON,  # the 1025th distinct id
        inbound.DUPLICATE,  # the second is still held
        inbound.HANDED_ON,  # only the oldest was dropped
    ]


def test_a_long_run_keeps_the_newest_1024_ids_and_nothing_older():
    router, _, _ = build()
    total = 3000
    route_ids(router, [ulid(n) for n in range(total)])
    first_held = total - inbound.DEDUPE_MAX_IDS
    assert route_ids(router, [ulid(total - 1), ulid(first_held), ulid(first_held - 1)]) == [
        inbound.DUPLICATE,
        inbound.DUPLICATE,
        inbound.HANDED_ON,
    ]


def test_duplicates_neither_renew_an_entry_nor_push_another_out():
    router, rec, clock = build()
    route_ids(router, [ulid(n) for n in range(inbound.DEDUPE_MAX_IDS)])
    route_ids(router, [ulid(inbound.DEDUPE_MAX_IDS - 1), ulid(0)] * 2000)  # a storm of re-sends: newest and oldest
    assert len(rec.delivered) == inbound.DEDUPE_MAX_IDS
    clock.now += inbound.DEDUPE_WINDOW_S
    assert route_ids(router, [ulid(0)]) == [inbound.HANDED_ON]  # the re-sends of id 0 did not extend its window


def test_a_busy_device_does_not_evict_the_ids_of_another_device():
    router, _, _ = build()
    route_ids(router, [ulid(1)], device="quiet")
    route_ids(router, [ulid(100 + n) for n in range(2 * inbound.DEDUPE_MAX_IDS)], device="busy")
    assert route_ids(router, [ulid(1)], device="quiet") == [inbound.DUPLICATE]


def test_ids_past_the_window_leave_the_table_also_when_the_cap_is_not_reached():
    router, _, clock = build()
    for n in range(10):
        route_ids(router, [ulid(n)])
        clock.now += 61  # ten ids over 610 s: the first falls out of the window before the last arrives
    assert route_ids(router, [ulid(0), ulid(9)]) == [inbound.HANDED_ON, inbound.DUPLICATE]


def with_deliver(rec: Recorder, deliver: Any) -> inbound.Inbound:
    """An ``Inbound`` over *rec*'s ports with its own ``deliver`` (the hand-on under test)."""
    ports = rec.ports()
    return inbound.Inbound(settings(), lambda: 0.0, inbound.Ports(ports.source, ports.unrouted, deliver, ports.refuse))


def test_identical_frames_that_race_are_handed_on_once():
    rec = Recorder()
    delivered = rec.ports().deliver

    async def slow(text: inbound.TextIn, source: Any) -> bool:
        await asyncio.sleep(0.05)  # the hand-on is still running when the copies arrive
        return await delivered(text, source)

    router = with_deliver(rec, slow)

    async def burst() -> list[str]:
        return list(await asyncio.gather(*(router.msg_in(DEVICE, WORK, frame()) for _ in range(25))))

    outcomes = asyncio.run(burst())
    assert sorted(outcomes) == sorted([inbound.HANDED_ON] + [inbound.DUPLICATE] * 24) and len(rec.delivered) == 1


def test_the_same_id_racing_from_two_devices_is_handed_on_twice():
    router, rec, _ = build()

    async def burst() -> list[str]:
        calls = [router.msg_in(d, WORK, frame()) for d in ("dev-a", "dev-b") for _ in range(5)]
        return list(await asyncio.gather(*calls))

    outcomes = asyncio.run(burst())
    assert outcomes.count(inbound.HANDED_ON) == 2 and sorted(t.user_id for t, _ in rec.delivered) == ["dev-a", "dev-b"]


def test_a_copy_that_races_a_failing_hand_on_is_dropped_and_the_re_send_afterwards_is_handed_on():
    rec = Recorder()
    works = rec.ports().deliver
    state: dict[str, Any] = {"release": None, "fail": True}

    async def deliver(text: inbound.TextIn, source: Any) -> bool:
        if state["fail"]:
            await state["release"].wait()
            raise RuntimeError("handle_message failed")
        return await works(text, source)

    router = with_deliver(rec, deliver)

    async def scenario() -> tuple[str, str, str]:
        state["release"] = asyncio.Event()
        first = asyncio.create_task(router.msg_in(DEVICE, WORK, frame()))
        await asyncio.sleep(0)
        copy_outcome = await router.msg_in(DEVICE, WORK, frame())  # while the first is in flight
        state["release"].set()
        (failure,) = await asyncio.gather(first, return_exceptions=True)
        state["fail"] = False  # the phone re-sends after reconnecting; handle_message works now
        return copy_outcome, type(failure).__name__, await router.msg_in(DEVICE, WORK, frame())

    assert asyncio.run(scenario()) == (inbound.DUPLICATE, "RuntimeError", inbound.HANDED_ON)
    assert len(rec.delivered) == 1


def test_when_the_hand_on_is_cancelled_the_id_is_released_and_the_re_send_is_handed_on():
    rec = Recorder()
    works = rec.ports().deliver
    state = {"hang": True}

    async def deliver(text: inbound.TextIn, source: Any) -> bool:
        if state["hang"]:
            await asyncio.sleep(30)
        return await works(text, source)

    router = with_deliver(rec, deliver)

    async def scenario() -> str:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(router.msg_in(DEVICE, WORK, frame()), 0.05)
        state["hang"] = False
        return await router.msg_in(DEVICE, WORK, frame())

    assert asyncio.run(scenario()) == inbound.HANDED_ON and len(rec.delivered) == 1


def test_discard_of_an_unknown_id_or_device_is_harmless_and_releases_only_that_id():
    router, _, _ = build()
    route_ids(router, [ulid(1), ulid(2)])
    router.dedupe.discard("never-seen", ulid(1))
    router.dedupe.discard(DEVICE, ulid(99))
    router.dedupe.discard(DEVICE, ulid(1))
    assert route_ids(router, [ulid(2), ulid(1)]) == [inbound.DUPLICATE, inbound.HANDED_ON]


@pytest.mark.parametrize(
    ("lane", "device_lanes"),
    [
        ("Work", WORK),  # case is part of the name
        ("glasses:work", WORK),  # the prefix belongs to chat_id, not to the lane
        ("work ", WORK),
        ("work\n", WORK),
        ("work", ()),  # a device with no lanes may send nowhere
        (None, ("work",)),  # no lane: the home lane (assistant) is not one of the device's lanes
    ],
    ids=[
        "case",
        "prefixed",
        "trailing blank",
        "trailing newline",
        "no device lanes",
        "home lane outside the device lanes",
    ],
)
def test_a_lane_that_is_not_exactly_an_allowed_lane_is_forbidden_before_the_gateway_is_asked(lane, device_lanes):
    rec = Recorder()
    asked: list[tuple[str, str]] = []
    ports = rec.ports()

    def source(chat_id: str, user_id: str) -> Any:
        asked.append((chat_id, user_id))
        return ports.source(chat_id, user_id)

    router = inbound.Inbound(
        settings(), lambda: 0.0, inbound.Ports(source, ports.unrouted, ports.deliver, ports.refuse)
    )
    assert asyncio.run(router.msg_in(DEVICE, device_lanes, frame(lane=lane))) == "lane_forbidden"
    assert asked == [] and rec.delivered == [] and [r[1] for r in rec.refused] == ["lane_forbidden"]


def test_an_unrouted_refusal_takes_no_dedupe_entry_so_the_re_send_after_the_route_is_fixed_is_handed_on():
    rec = Recorder(unrouted=frozenset({"glasses:work"}))
    router = inbound.Inbound(settings(), lambda: 0.0, rec.ports())

    async def scenario() -> list[str]:
        first = await router.msg_in(DEVICE, WORK, frame())
        rec.unrouted_chats = frozenset()
        return [first, await router.msg_in(DEVICE, WORK, frame())]

    assert asyncio.run(scenario()) == ["lane_unrouted", inbound.HANDED_ON] and len(rec.delivered) == 1


def test_a_forbidden_lane_wins_over_an_unrouted_one():
    rec = Recorder(unrouted=frozenset({"glasses:other"}))
    router = inbound.Inbound(settings(), lambda: 0.0, rec.ports())
    assert asyncio.run(router.msg_in(DEVICE, ("other",), frame(lane="other"))) == "lane_forbidden"  # not in config
    assert [r[1] for r in rec.refused] == ["lane_forbidden"] and rec.delivered == []


def test_a_hand_on_the_gateway_drops_releases_the_id_so_the_re_send_is_handed_on_once():
    rec = Recorder()
    router = inbound.Inbound(settings(), lambda: 0.0, rec.ports())

    async def scenario() -> list[str]:
        rec.gate_open = True  # deliver reports False: the allow-all gate dropped the event
        dropped = await router.msg_in(DEVICE, WORK, frame())
        rec.gate_open = False
        return [dropped, await router.msg_in(DEVICE, WORK, frame()), await router.msg_in(DEVICE, WORK, frame())]

    assert asyncio.run(scenario()) == [inbound.NOT_HANDED_ON, inbound.HANDED_ON, inbound.DUPLICATE]
    assert len(rec.delivered) == 2  # the dropped attempt and the re-send; the third copy never reached deliver
