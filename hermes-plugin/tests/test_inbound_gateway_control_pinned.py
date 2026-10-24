# SPDX-License-Identifier: Apache-2.0
"""Glasses text never controls the pinned gateway (T1.08): approvals come only through the T1.10 card. The real
``GlassesAdapter`` in the Hermes venv (as in ``test_inbound_pinned_hermes.py``) builds the event of each ``msg.in``;
each event then goes through the pinned base ``handle_message`` while its session is busy and a dangerous-command
approval blocks it, with the pinned runner's busy handler (``GatewayRunner._handle_active_session_busy_message``, the
plain-text approval route of ``gateway/run_busy.py``) wired as in the gateway. ``/approve always`` and ``/yolo`` are
no command and are not dispatched inline; ``yes``, ``always``, ``session`` and ``approve`` resolve nothing and the
approval stays pending. The control run sends the same event with ``allow_gateway_control=True`` (the pinned default)
and shows the same setup does resolve the approval and does dispatch the commands, so the check is not vacuous.
Skipped (named reason) without the venv; scratch ``HERMES_HOME``, no gateway, no network."""

from __future__ import annotations

from test_inbound_pinned_hermes import DEVICE_ID, SETUP, _run

TEXTS = ("/approve always", "/yolo", "yes", "always", "session", "approve")

RUNNER = (
    SETUP
    + f"""
import dataclasses
from hermes_cli.commands import should_bypass_active_session
from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry

os.environ["GLASSES_ALLOWED_USERS"] = {DEVICE_ID!r}  # the device is allowed: the busy path's user check passes
inline = []

async def message_handler(event):  # the runner's handler: an active session reaches it inline only for a command
    inline.append(event.text)

async def busy(event, key):
    text, command = event.text, event.get_command()
    entry = _ApprovalEntry({{"command": "probe-command", "pattern_key": "probe", "description": "probe"}})
    approval._gateway_queues[key] = [entry]
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = asyncio.create_task(asyncio.sleep(60))  # a live turn holds the session
    inline.clear()
    try:
        await base_handle_message(adapter, event)  # the pinned base path
        return {{"text": text, "is_command": event.is_command() if event.text == text else None,
                "command": command, "bypass": should_bypass_active_session(command), "inline": list(inline),
                "resolved": entry.result, "pending": approval.has_blocking_approval(key)}}
    finally:
        approval._gateway_queues.pop(key, None)
        adapter._session_tasks.pop(key).cancel()
        adapter._active_sessions.pop(key, None)
        adapter._pending_messages.pop(key, None)

async def main():
    for n, text in enumerate({list(TEXTS)!r}):
        await adapter.router.on_frame(device, msg_in(f"01JABCDEFGHJKMNPQRSTVWX{{n:03d}}", "work", text))
    adapter.set_message_handler(message_handler)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    rows = []
    for event in seen:
        key = adapter._event_session_key(event)
        control = dataclasses.replace(event, allow_gateway_control=True)
        rows.append({{"flag": event.allow_gateway_control, "glasses": await busy(event, key),
                     "control": await busy(control, key)}})
    await adapter.disconnect()
    return rows

print(json.dumps(asyncio.run(main())))
"""
)


def test_glasses_text_is_no_gateway_command_and_resolves_no_pending_approval():
    rows = _run(RUNNER)
    assert [row["glasses"]["text"] for row in rows] == list(TEXTS)
    for row in rows:
        glasses = row["glasses"]
        assert row["flag"] is False, row
        assert (glasses["is_command"], glasses["command"], glasses["bypass"]) == (False, None, False), row
        assert glasses["inline"] == [] and glasses["resolved"] is None and glasses["pending"] is True, row
    slash, words = rows[:2], rows[2:]
    assert [row["control"]["inline"] for row in slash] == [["/approve always"], ["/yolo"]]
    assert [row["control"]["resolved"] for row in words] == ["once", "always", "session", "once"]
    assert all(row["control"]["pending"] is False for row in words)
