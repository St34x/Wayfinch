# SPDX-License-Identifier: Apache-2.0
"""More glasses text that must stay text at the pinned gateway (T1.08): slash commands in odd spellings, the plain
phrase the gateway rewrites into ``/restart`` for a DM, and approval words. The real ``GlassesAdapter`` builds the
event of each ``msg.in`` (``test_inbound_pinned_hermes.SETUP``); the pinned base ``handle_message`` then takes it on an
idle session and the message handler the runner would install sees the text unchanged and no command. The control
run hands the same texts over with ``allow_gateway_control=True`` (the pinned default) and shows the rewrite and the
command parse do happen, so the check is not vacuous. Skipped (named reason) without the venv; no network."""

from __future__ import annotations

from test_inbound_pinned_hermes import DEVICE_ID, SETUP, _run

TEXTS = (
    "restart gateway",
    "Restart the gateway",
    "/restart",
    "/new",
    "/reset",
    "/stop",
    "/update",
    "/model --global some-model",
    "  /yolo",
    "/APPROVE ALWAYS",
    "\n/approve once",
    "/approve\u200b always",
    "approve",
    "deny",
)

RUNNER = (
    SETUP
    + f"""
import dataclasses

os.environ["GLASSES_ALLOWED_USERS"] = {DEVICE_ID!r}
handled = []

async def message_handler(event):  # the runner's handler on an idle session: what the agent turn would receive
    handled.append((event.text, event.get_command(), event.is_command()))

async def hand_over(event):
    handled.clear()
    adapter._active_sessions.pop(adapter._event_session_key(event), None)
    await base_handle_message(adapter, event)  # the pinned base path; it spawns the turn as a background task
    await asyncio.sleep(0.05)
    for task in list(adapter._background_tasks):
        await asyncio.wait({{task}}, timeout=2)
    return {{"accepted": event._gateway_accepted, "handled": list(handled)}}

async def main():
    for n, text in enumerate({list(TEXTS)!r}):
        await adapter.router.on_frame(device, msg_in(f"01JABCDEFGHJKMNPQRSTVWX{{n:03d}}", "work", text))
    adapter.set_message_handler(message_handler)
    rows = []
    for event in seen:
        control = dataclasses.replace(event, allow_gateway_control=True)
        rows.append({{"text": event.text, "flag": event.allow_gateway_control,
                     "is_command": event.is_command(), "command": event.get_command(),
                     "glasses": await hand_over(event), "control": await hand_over(control)}})
    await adapter.disconnect()
    return rows

print(json.dumps(asyncio.run(main())))
"""
)


def test_glasses_text_reaches_the_agent_unchanged_and_is_no_command_on_an_idle_session():
    rows = _run(RUNNER)
    assert [row["text"] for row in rows] == list(TEXTS)
    for row in rows:
        assert row["flag"] is False and row["is_command"] is False and row["command"] is None, row
        assert row["glasses"]["accepted"] is True, row
        assert row["glasses"]["handled"] == [[row["text"], None, False]], row  # the turn gets the text, unrewritten


def test_the_same_texts_with_gateway_control_on_are_rewritten_or_parsed_so_the_check_is_not_vacuous():
    rows = {row["text"]: row for row in _run(RUNNER)}
    assert rows["restart gateway"]["control"]["handled"][0][0] == "/restart"
    assert rows["/stop"]["control"]["handled"][0][1] == "stop"
    assert rows["/APPROVE ALWAYS"]["control"]["handled"][0][1] == "approve"
