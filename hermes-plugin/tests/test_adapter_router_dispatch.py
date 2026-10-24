# SPDX-License-Identifier: Apache-2.0
"""The frame router moved out of ``adapter.py`` (T1.249): what each frame type does in ``on_frame``, a reply before the
router is bound, a refusal with no connection table, ``hand_on``'s answer, and the logger name its records keep."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from server_helpers import MSG_IN, SCHEMA

from glasses import inbound

from test_adapter import ADAPTER_MODULE, adapter_module, new_adapter  # noqa: F401 - the fixture is used by name

DEVICE = "0123456789abcdef"
LANES = ("assistant", "work")
EXTRA = {"lanes": ["assistant", "work"], "home_lane": "assistant"}
MARKER = "planted-body-8841"
device = SimpleNamespace(device_id=DEVICE, lanes=LANES)


class Spy:
    """Stands in for the outbox (``ack_soon``) and the inbound text path (``msg_in``)."""

    def __init__(self) -> None:
        self.acks: list[tuple[str, int]] = []
        self.msg_in_calls: list[Any] = []

    def ack_soon(self, device_id: str, up_to: int) -> None:
        self.acks.append((device_id, up_to))

    async def msg_in(self, device_id: str, lanes: Any, frame: Any) -> str:
        self.msg_in_calls.append((device_id, tuple(lanes), frame["id"]))
        return "spy"


def _routed(adapter_module, with_outbox: bool):  # noqa: F811 - the imported fixture
    adapter = new_adapter(adapter_module, EXTRA)
    spy = Spy()
    adapter.router.text.msg_in = spy.msg_in  # type: ignore[method-assign]
    adapter.router.bind(SimpleNamespace(send=lambda *_: True), SCHEMA, spy if with_outbox else None)  # type: ignore[arg-type]
    return adapter, spy


def _on_frame(adapter: Any, frame: Any) -> None:
    asyncio.run(adapter.router.on_frame(device, frame))


def test_an_ack_goes_to_the_outbox_once_and_never_to_the_text_path(adapter_module):  # noqa: F811 - the imported fixture, used by name
    adapter, spy = _routed(adapter_module, with_outbox=True)
    _on_frame(adapter, {"type": "ack", "body": {"upToSeq": 7}})
    assert spy.acks == [(DEVICE, 7)] and spy.msg_in_calls == []


def test_an_ack_without_a_bound_outbox_is_logged_by_type_and_never_reaches_the_text_path(adapter_module, caplog):  # noqa: F811 - the imported fixture, used by name
    adapter, spy = _routed(adapter_module, with_outbox=False)
    with caplog.at_level(logging.DEBUG):
        _on_frame(adapter, {"type": "ack", "body": {"upToSeq": 7}})
    lines = [r for r in caplog.records if "frame not dispatched" in r.getMessage()]
    assert spy.acks == [] and spy.msg_in_calls == []
    assert [(r.name, r.getMessage()) for r in lines] == [
        (ADAPTER_MODULE, "glasses adapter: ack frame not dispatched (dispatch not built yet)")
    ]


def test_a_frame_of_another_type_is_logged_by_type_only_and_dispatched_nowhere(adapter_module, caplog):  # noqa: F811 - the imported fixture, used by name
    adapter, spy = _routed(adapter_module, with_outbox=True)
    with caplog.at_level(logging.DEBUG):
        _on_frame(adapter, {"type": "audio.start", "body": {"note": MARKER}})
    assert spy.acks == [] and spy.msg_in_calls == []
    assert "audio.start frame not dispatched" in caplog.text and MARKER not in caplog.text


def test_a_msg_in_goes_to_the_text_path_with_the_devices_lanes_and_not_to_the_outbox(adapter_module):  # noqa: F811 - the imported fixture, used by name
    adapter, spy = _routed(adapter_module, with_outbox=True)
    _on_frame(adapter, {**MSG_IN, "lane": "work"})
    assert spy.msg_in_calls == [(DEVICE, LANES, MSG_IN["id"])] and spy.acks == []


def test_a_reply_before_the_router_is_bound_is_refused_and_nothing_is_logged_as_a_cut(adapter_module, caplog):  # noqa: F811 - the imported fixture, used by name
    adapter = new_adapter(adapter_module, EXTRA)
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="frame router not bound"):
        adapter.router.answer("glasses:work", "x" * 4000, None)
    assert not [r for r in caplog.records if "cut" in r.getMessage()]


def test_a_refusal_before_the_router_is_bound_sends_nothing_and_after_it_is_one_error_frame(adapter_module):  # noqa: F811 - the imported fixture, used by name
    adapter = new_adapter(adapter_module, EXTRA)
    sent: list[tuple[str, str]] = []
    frame: Any = {**MSG_IN, "lane": "not-a-lane-of-the-device"}

    async def scenario() -> list[str]:
        unbound = await adapter.router.text.msg_in(DEVICE, LANES, frame)
        adapter.router.bind(SimpleNamespace(send=lambda d, text: sent.append((d, text)) or True), SCHEMA)  # type: ignore[arg-type]
        return [unbound, await adapter.router.text.msg_in(DEVICE, LANES, frame)]

    assert asyncio.run(scenario()) == [inbound.LANE_FORBIDDEN.code, inbound.LANE_FORBIDDEN.code]
    assert [d for d, _ in sent] == [DEVICE]
    error = json.loads(sent[0][1])
    assert error["type"] == "error" and error["body"]["code"] == "lane_forbidden"


def test_hand_on_says_whether_the_event_reached_the_gateway_and_handle_message_follows_it(adapter_module, monkeypatch):  # noqa: F811 - the imported fixture, used by name
    adapter = new_adapter(adapter_module, EXTRA)
    first, second = SimpleNamespace(_gateway_accepted=None), SimpleNamespace(_gateway_accepted=None)
    assert asyncio.run(adapter.hand_on(first)) is True and adapter.handed_on == [first]
    monkeypatch.setenv("GLASSES_ALLOWED_USERS", "dev_1,*")  # the allow-all gate is open
    assert asyncio.run(adapter.hand_on(second)) is False
    assert asyncio.run(adapter.handle_message(second)) is None
    assert adapter.handed_on == [first] and second._gateway_accepted is False


def test_the_open_gate_logs_the_first_drop_at_warning_and_the_rest_at_debug(adapter_module, monkeypatch, caplog):  # noqa: F811 - the imported fixture, used by name
    adapter = new_adapter(adapter_module, EXTRA)
    monkeypatch.setenv("GLASSES_ALLOWED_USERS", "dev_1,*")
    with caplog.at_level(logging.DEBUG, logger=ADAPTER_MODULE):
        for _ in range(3):
            asyncio.run(adapter.hand_on(SimpleNamespace(_gateway_accepted=None)))
    drops = [r.levelno for r in caplog.records if "inbound event dropped" in r.getMessage()]
    assert drops == [logging.WARNING, logging.DEBUG, logging.DEBUG]
