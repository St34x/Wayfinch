# SPDX-License-Identifier: Apache-2.0
"""A signer revoke of a device with an open session, against the pinned Hermes (T1.248): the real ``GlassesAdapter``
in the Hermes venv (``.local/venv314``, clone at ``HERMES_REF``) behind the real WS server on loopback, with the frame
check wired as ``connect()`` wires it (``revoked_ids.RevokedIds``) and a real ``Registry`` whose ``revoke`` replaces
``devices.json`` by an atomic rename. A ``msg.in`` before the revoke reaches the pinned base class's
``handle_message``; the one sent after it does not, the session ends ``4403`` within a second, and the device's
outbox rows are gone. Skipped (named reason) without the venv; a scratch ``HERMES_HOME``, no gateway, no network."""

from __future__ import annotations

import json
import subprocess

from server_auth_helpers import FP

from conftest import PLUGIN_ROOT
from test_inbound_pinned_hermes import SETUP, _pinned_ref, _run

FIXTURES = PLUGIN_ROOT.parent / "protocol" / "fixtures" / "valid"
SAID = "planted-said-after-revoke-6604"

BODY = """
import time
from pathlib import Path
import aiohttp
from aiohttp import web
from glasses.outbox import Outbox, Store
from glasses.registry import Registry
from glasses.revoked_ids import RevokedIds
from glasses.server import CONNECTIONS, ServerDeps, build_app, build_runner

FP, HELLO, PING, SAID = @FP@, @HELLO@, @PING@, @SAID@
state = Path(os.environ["HERMES_HOME"]) / "glasses-state"
state.mkdir()
state.chmod(0o700)  # the only supported mode, whatever the umask (0002 gives 0775, which the outbox refuses)
registry = Registry(state / "registry")
dev, token = registry.add("glasses", FP, ["work"])
box = Outbox(Store(state / "outbox.sqlite", time.time, lambda seq: ""), adapter.router.text.dedupe.forget,
             registry.list)
check = RevokedIds(registry._file, registry.list, lambda _id: False, box.is_revoked)
deps = ServerDeps(gate_ok_async=adapter.gate_ok_async, registry=registry, is_revoked=check.is_revoked,
                  peer_allowed=lambda request: True, client_fingerprint=lambda request: FP,
                  on_frame=adapter.router.on_frame, schema=schema, max_audio_sec=30, on_hello=box.hello,
                  on_revoked=box.on_revoked(registry.list))


async def until(ws, kind):
    while (frame := await ws.receive_json(timeout=5))["type"] != kind:
        pass


async def main():
    await check.load()
    app = build_app(deps)
    adapter.router.bind(app[CONNECTIONS], schema, box)
    check.bind(app[CONNECTIONS].close_device, box.purge_soon)
    served = build_runner(app)
    await served.setup()
    site = web.TCPSite(served, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:%d/ws" % served.addresses[0][1]
    try:
        async with aiohttp.ClientSession() as session:
            ws = await session.ws_connect(url, headers={"Authorization": "Bearer " + token})
            await ws.send_str(json.dumps(HELLO))
            await until(ws, "welcome")
            await ws.send_str(json.dumps(msg_in("01JABCDEFGHJKMNPQRSTVWXYZ1", "work")))
            await ws.send_str(json.dumps(PING))
            await until(ws, "pong")
            before = len(seen)
            box_rows = await adapter.send("glasses:work", "answer before the revoke")
            registry.revoke(dev.device_id)
            changed = time.monotonic()
            await ws.send_str(json.dumps(msg_in("01JABCDEFGHJKMNPQRSTVWXYZ2", "work", SAID)))
            while not ws.closed:
                message = await ws.receive(timeout=3)
                if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    break
            return before, ws.close_code, time.monotonic() - changed, box_rows.success
    finally:
        await served.cleanup()
        await box.close()
        adapter._edge = None
        await adapter.disconnect()


adapter._edge = SimpleNamespace(outbox=box, registry=registry)
before, code, elapsed, stored = asyncio.run(main())
import sqlite3
with sqlite3.connect(state / "outbox.sqlite") as db:
    left = db.execute("SELECT count(*) FROM rows").fetchone()[0]
print(json.dumps({
    "gateway": os.path.dirname(os.path.dirname(gateway.platforms._shared.__file__)),
    "before": before, "after": len(seen), "said_seen": any(event.text == SAID for event in seen),
    "code": code, "elapsed": elapsed, "stored": stored, "rows_left": left,
}))
"""


def _runner() -> str:
    values = {
        "@FP@": repr(FP),
        "@HELLO@": repr(json.loads((FIXTURES / "hello.json").read_text(encoding="utf-8"))),
        "@PING@": repr(json.loads((FIXTURES / "ping.json").read_text(encoding="utf-8"))),
        "@SAID@": repr(SAID),
    }
    body = BODY
    for name, value in values.items():
        body = body.replace(name, value)
    return SETUP + body


def test_pinned_hermes_gets_no_msg_in_from_a_device_the_signer_revoked_while_its_session_was_open():
    result = _run(_runner())
    head = subprocess.run(
        ["git", "-C", result["gateway"], "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head.startswith(_pinned_ref()), f"the venv's Hermes is at {head}, not HERMES_REF {_pinned_ref()}"
    assert result["before"] == 1 and result["stored"] is True  # the session worked before the revoke
    assert result["after"] == 1 and result["said_seen"] is False  # nothing said after the revoke was handed on
    assert result["code"] == 4403 and result["elapsed"] < 1.0 and result["rows_left"] == 0
