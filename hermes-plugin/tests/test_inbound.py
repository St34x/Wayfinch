# SPDX-License-Identifier: Apache-2.0
"""``glasses.inbound`` (T1.08): lane checks, the dedupe table and the event fields of a ``msg.in``, with recording ports
(the stand-in ``handle_message`` is ``deliver``). The path through the adapter and the WS server is in
``test_inbound_session.py``, the pinned Hermes in ``test_inbound_pinned_hermes.py``."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest
from server_helpers import MSG_IN, SCHEMA

from glasses import config as gcfg
from glasses import inbound
from glasses.envelope_encode import encode

DEVICE = "dev-a"
TEXT = "planted-text-5531"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    """Ports that record: ``delivered`` (TextIn, source), ``refused`` (device, code, message, corr)."""

    def __init__(self, unrouted: frozenset[str] = frozenset()) -> None:
        self.unrouted_chats = unrouted
        self.delivered: list[tuple[inbound.TextIn, Any]] = []
        self.refused: list[tuple[str, str, str, str]] = []
        self.fail = False  # deliver raises, as a failing handle_message would
        self.gate_open = False  # deliver reports a drop, as the adapter does while the allow-all gate is open

    def ports(self) -> inbound.Ports:
        def source(chat_id: str, user_id: str) -> Any:
            return SimpleNamespace(chat_id=chat_id, user_id=user_id, rejected=chat_id in self.unrouted_chats)

        async def deliver(text: inbound.TextIn, src: Any) -> bool:
            if self.fail:
                raise RuntimeError("handle_message failed")
            self.delivered.append((text, src))
            return not self.gate_open  # False: the gateway dropped the event (allow-all gate open)

        def refuse(device_id: str, refusal: inbound.Refusal, corr: str) -> None:
            self.refused.append((device_id, refusal.code, refusal.message, corr))

        return inbound.Ports(source, lambda src: src.rejected, deliver, refuse)


def settings(**values: Any) -> gcfg.GlassesConfig:
    return gcfg.load({"lanes": ["assistant", "work"], "home_lane": "assistant", **values})


def build(cfg: gcfg.GlassesConfig | None = None, unrouted: frozenset[str] = frozenset()):
    recorder, clock = Recorder(unrouted), Clock()
    return inbound.Inbound(cfg or settings(), clock, recorder.ports()), recorder, clock


def msg(frame_id: str = MSG_IN["id"], lane: str | None = None, text: str = TEXT) -> Any:
    frame = copy.deepcopy(MSG_IN)
    frame["id"], frame["body"]["text"] = frame_id, text
    if lane is not None:
        frame["lane"] = lane
    assert SCHEMA.is_valid(frame)
    return frame


def ulid(n: int) -> str:
    return f"01J{n:023d}"


def route(router: inbound.Inbound, frame: Any, device: str = DEVICE, lanes=("assistant", "work")) -> str:
    return asyncio.run(router.msg_in(device, tuple(lanes), frame))


def test_a_msg_in_on_an_allowed_lane_is_handed_on_once_with_the_event_fields():
    router, rec, _ = build()
    assert route(router, msg(lane="work")) == inbound.HANDED_ON
    ((text, source),) = rec.delivered
    assert text == inbound.TextIn("glasses:work", DEVICE, TEXT, MSG_IN["id"])
    assert (source.chat_id, source.user_id) == ("glasses:work", DEVICE) and rec.refused == []


def test_a_msg_in_without_lane_goes_to_the_home_lane():
    router, rec, _ = build(settings(home_lane="glasses:work"))
    route(router, msg())
    assert [text.chat_id for text, _ in rec.delivered] == ["glasses:work"]


@pytest.mark.parametrize(
    ("lane", "device_lanes"),
    [("other", ("assistant", "work", "other")), ("work", ("assistant",))],
    ids=["not in the config lanes", "not in the device's lanes"],
)
def test_a_lane_not_allowed_is_lane_forbidden_with_corr_and_no_call(lane, device_lanes):
    router, rec, _ = build()
    assert route(router, msg(lane=lane), lanes=device_lanes) == "lane_forbidden"
    assert rec.delivered == []
    assert rec.refused == [(DEVICE, "lane_forbidden", inbound.LANE_FORBIDDEN.message, MSG_IN["id"])]


def test_empty_config_lanes_allow_the_devices_lanes_only():
    router, rec, _ = build(settings(lanes=[], home_lane=""))
    assert route(router, msg(lane="other"), lanes=("other",)) == inbound.HANDED_ON
    assert route(router, msg(ulid(1), lane="work"), lanes=("other",)) == "lane_forbidden"
    assert route(router, msg(ulid(2)), lanes=("other",)) == "lane_forbidden"  # no lane and no home lane
    assert len(rec.delivered) == 1


def test_a_lane_routed_to_a_profile_the_gateway_does_not_serve_is_lane_unrouted_and_no_call():
    router, rec, _ = build(unrouted=frozenset({"glasses:work"}))
    assert route(router, msg(lane="work")) == "lane_unrouted"
    assert rec.delivered == []
    assert rec.refused == [(DEVICE, "lane_unrouted", inbound.LANE_UNROUTED.message, MSG_IN["id"])]


def test_the_same_id_twice_within_ten_minutes_is_one_call_and_after_ten_minutes_a_second():
    router, rec, clock = build()
    assert [route(router, msg()), route(router, msg())] == [inbound.HANDED_ON, inbound.DUPLICATE]
    clock.now += inbound.DEDUPE_WINDOW_S - 1
    assert route(router, msg()) == inbound.DUPLICATE  # a duplicate does not renew the entry
    clock.now += 1
    assert route(router, msg()) == inbound.HANDED_ON
    assert len(rec.delivered) == 2 and rec.refused == []


def test_the_table_keeps_the_newest_1024_ids_of_a_device():
    router, rec, _ = build()
    for n in range(inbound.DEDUPE_MAX_IDS + 1):
        route(router, msg(ulid(n)))
    assert route(router, msg(ulid(1))) == inbound.DUPLICATE  # the second id is still held: 1024 kept
    assert route(router, msg(ulid(0))) == inbound.HANDED_ON  # the first was dropped
    assert len(rec.delivered) == inbound.DEDUPE_MAX_IDS + 2


def test_the_same_id_from_two_devices_is_two_calls_and_forget_clears_one_device():
    router, rec, _ = build()
    route(router, msg(), device="dev-a")
    route(router, msg(), device="dev-b")
    router.dedupe.forget("dev-a")
    router.dedupe.forget("never-seen")
    assert route(router, msg(), device="dev-a") == inbound.HANDED_ON
    assert route(router, msg(), device="dev-b") == inbound.DUPLICATE
    assert [text.user_id for text, _ in rec.delivered] == ["dev-a", "dev-b", "dev-a"]


def test_a_refused_frame_takes_no_dedupe_entry():
    router, rec, _ = build()
    route(router, msg(lane="other"))
    assert route(router, msg(lane="work")) == inbound.HANDED_ON and len(rec.delivered) == 1


@pytest.mark.parametrize("refusal", [inbound.LANE_FORBIDDEN, inbound.LANE_UNROUTED])
def test_the_error_frame_is_server_encodable_without_seq_with_corr_and_no_text(refusal):
    frame = inbound.error_for(refusal, MSG_IN["id"], ulid(7), "2026-10-21T00:00:00Z")
    sent = json.loads(encode(frame, SCHEMA))
    assert sent["corr"] == MSG_IN["id"] and "seq" not in sent and "lane" not in sent
    assert sent["body"] == {"code": refusal.code, "message": refusal.message}


def test_the_user_id_is_the_device_id_given_never_a_device_or_user_field_of_the_frame():
    router, rec, _ = build()
    forged = msg(lane="work", text="deviceId=dev-forged userId=dev-forged")
    forged.update(deviceId="dev-forged", userId="dev-forged", user_id="dev-forged", device="dev-forged")
    forged["body"].update(deviceId="dev-forged", user_id="dev-forged")  # past the schema: inbound must not read them
    route(router, forged, device="dev-registry-id")
    ((text, source),) = rec.delivered
    assert (text.user_id, source.user_id) == ("dev-registry-id", "dev-registry-id")


def test_two_ids_that_expire_together_are_both_purged_and_the_second_is_handed_on_again():
    router, rec, clock = build()
    route(router, msg(ulid(1)))
    route(router, msg(ulid(2)))
    clock.now += inbound.DEDUPE_WINDOW_S
    assert route(router, msg(ulid(2))) == inbound.HANDED_ON  # this one call purges both expired ids
    assert [text.message_id for text, _ in rec.delivered] == [ulid(1), ulid(2), ulid(2)]


def test_when_the_hand_on_raises_the_id_is_released_and_the_re_send_is_handed_on():
    router, rec, _ = build()
    rec.fail = True
    with pytest.raises(RuntimeError, match="handle_message failed"):
        route(router, msg(lane="work"))
    rec.fail = False
    assert route(router, msg(lane="work")) == inbound.HANDED_ON
    assert route(router, msg(lane="work")) == inbound.DUPLICATE  # handed on once now: the id is held again
    assert len(rec.delivered) == 1
