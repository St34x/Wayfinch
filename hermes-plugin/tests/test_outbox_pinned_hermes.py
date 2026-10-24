# SPDX-License-Identifier: Apache-2.0
"""The gateway path into ``send()`` against the pinned Hermes (T1.09b): the real ``GlassesAdapter`` in the Hermes venv
(``.local/venv314``, clone at ``HERMES_REF``) with the pinned ``BasePlatformAdapter.handle_message`` (not a stand-in):
a ``msg.in`` becomes the event, the base class runs the turn with a message handler that answers, and the answer the
base class hands to ``send()`` lands as one ``msg.out`` on the device's welcomed link with ``corr`` = the ``msg.in`` id
and ``chatId`` = ``glasses:<lane>``, stored in the outbox with ``seq`` 1. Skipped (named reason) without the venv; a
scratch ``HERMES_HOME``, no gateway, no network. ``SETUP`` is ``test_inbound_pinned_hermes``'s."""

from __future__ import annotations

from test_inbound_pinned_hermes import SETUP, _run

ANSWER = "planted-answer-8820"
MSG_ID = "01JABCDEFGHJKMNPQRSTVWXYZ0"

RUNNER = (
    SETUP
    + f"""
import time
from pathlib import Path
from glasses.outbox import Outbox, Store

base.BasePlatformAdapter.handle_message = base_handle_message  # the pinned base class, not the recorder


class Link:
    def __init__(self):
        self.frames = []

    def send(self, text):
        self.frames.append(json.loads(text))
        return True

    def refused(self):
        raise AssertionError("refused")


async def answer(event):
    return {ANSWER!r}

adapter.set_message_handler(answer)
calls = []
real_send = adapter.send


async def spy(chat_id, content, reply_to=None, metadata=None):
    calls.append([chat_id, content == {ANSWER!r}, reply_to])
    return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

adapter.send = spy
state = Path(os.environ["HERMES_HOME"]) / "glasses-state"
box = Outbox(Store(state / "outbox.sqlite", time.time, lambda seq: ""), adapter.router.text.dedupe.forget,
             lambda: [device])
adapter._edge = SimpleNamespace(outbox=box, registry=SimpleNamespace(list=lambda: [device]))
table = SimpleNamespace(send=lambda device_id, text: sent.append((device_id, json.loads(text))))
adapter.router.bind(table, schema, box)
link = Link()


async def main():
    await box.hello(device.device_id, 0, link)
    await adapter.router.on_frame(device, msg_in({MSG_ID!r}, "work"))
    for _ in range(400):
        if link.frames:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    adapter._edge = None
    await box.close()
    await adapter.disconnect()

asyncio.run(main())
resume, queued, last, _ = Store(state / "outbox.sqlite", time.time, lambda seq: "").hello(device.device_id, 0)
print(json.dumps({{
    "gateway": os.path.dirname(os.path.dirname(gateway.platforms._shared.__file__)),
    "calls": calls, "frames": link.frames, "stored": [seq for seq, _ in queued], "errors": sent,
}}))
"""
)


def test_the_pinned_gateway_hands_the_answer_to_send_and_it_lands_as_one_msg_out_with_corr():
    result = _run(RUNNER)
    assert result["calls"] == [["glasses:work", True, MSG_ID]], result
    ((frame),) = result["frames"]
    assert frame["type"] == "msg.out" and frame["seq"] == 1 and frame["corr"] == MSG_ID
    assert frame["body"] == {"kind": "answer", "text": ANSWER, "chatId": "glasses:work", "final": True}
    assert result["stored"] == [1] and result["errors"] == []  # still queued (no ack) with its seq
