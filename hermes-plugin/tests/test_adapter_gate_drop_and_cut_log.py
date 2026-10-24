# SPDX-License-Identifier: Apache-2.0
"""Through the adapter: a ``msg.in`` the open allow-all gate drops keeps no dedupe id (the re-send is handed on once the
gate is closed), and a reply cut to 2048 bytes logs the two byte counts and never the text."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from server_helpers import MSG_IN, SCHEMA

from glasses import inbound

from test_adapter import ADAPTER_MODULE, adapter_module, new_adapter  # noqa: F401 - the fixture is used by name

DEVICE = "0123456789abcdef"
LANES = ("assistant", "work")
EXTRA = {"lanes": ["assistant", "work"], "home_lane": "assistant"}


def test_a_msg_in_dropped_by_the_open_gate_is_handed_on_when_the_phone_sends_it_again_after_the_gate_closed(
    adapter_module,  # noqa: F811 - the imported fixture
):
    adapter = new_adapter(adapter_module, EXTRA)
    gate = {"open": True}
    adapter.gate_ok = lambda: not gate["open"]
    frame: Any = {**MSG_IN, "lane": "work"}

    async def scenario() -> list[str]:
        outcomes = [await adapter.router.text.msg_in(DEVICE, LANES, frame)]
        gate["open"] = False
        outcomes.append(await adapter.router.text.msg_in(DEVICE, LANES, frame))
        outcomes.append(await adapter.router.text.msg_in(DEVICE, LANES, frame))  # now a duplicate
        return outcomes

    assert asyncio.run(scenario()) == [inbound.NOT_HANDED_ON, inbound.HANDED_ON, inbound.DUPLICATE]
    assert len(adapter.handed_on) == 1


def test_a_reply_over_2048_bytes_logs_both_byte_counts_and_no_log_record_holds_the_text(adapter_module, caplog):  # noqa: F811 - the imported fixture
    adapter = new_adapter(adapter_module, EXTRA)
    adapter.router.bind(SimpleNamespace(send=lambda *_: True), SCHEMA)  # type: ignore[arg-type]
    reply = "planted-reply-6402-" * 200  # 3800 bytes
    with caplog.at_level(logging.DEBUG):
        adapter.router.answer("glasses:work", reply, None)
    assert [r.getMessage() for r in caplog.records if r.name == ADAPTER_MODULE and "cut" in r.getMessage()] == [
        "glasses adapter: reply cut from 3800 to 2048 bytes"
    ]
    assert not any("planted-reply" in r.getMessage() or "planted-reply" in str(r.args) for r in caplog.records)
