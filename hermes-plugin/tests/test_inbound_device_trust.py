# SPDX-License-Identifier: Apache-2.0
"""Device trust and failure paths of the inbound text (T1.08), through the real WS server and the adapter's frame
router (stand-in Hermes base class, as in ``test_inbound_session.py``): an unknown or revoked device never gets a
``msg.in`` handed on, an error frame reaches only the device whose frame it answers, a failing or stalled hand-on ends
or bounds the session without the text in any log, and the phone's re-send after the failure is processed."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType
from server_auth_helpers import creds, new_fingerprints, upgrade_headers
from server_helpers import HELLO, MSG_IN, PING, SCHEMA, serving

from glasses.server import CONNECTIONS, build_app

from test_adapter import FakeBaseAdapter, adapter_module, new_adapter, router_module  # noqa: F401 - fixtures by name
from test_inbound_session import LANES, TEXT, msg, rig, run, until_pong


@pytest.fixture
def module(adapter_module):  # noqa: F811 - the imported fixture
    return adapter_module


SECOND_ID = "01JABCDEFGHJKMNPQRSTVWXYZ1"


async def serve_and(adapter: Any, w: Any, scenario: Any, **changes: Any) -> Any:
    """The app with the adapter's router as ``on_frame``; *scenario(port)* connects as it likes."""
    changes.setdefault("on_frame", adapter.router.on_frame)
    app = build_app(w.deps(**changes))
    adapter.router.bind(app[CONNECTIONS], SCHEMA)
    try:
        async with serving(app) as port:
            return await scenario(port)
    finally:
        await adapter.disconnect()


async def hello(session: aiohttp.ClientSession, port: int, headers: dict[str, str]) -> aiohttp.ClientWebSocketResponse:
    ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=headers)
    await ws.send_str(json.dumps(HELLO))
    assert (await ws.receive_json())["type"] == "welcome"
    return ws


def test_an_unknown_device_cannot_open_a_session_and_nothing_is_handed_on(module, tmp_path):
    adapter, w = rig(module, tmp_path)

    async def scenario(port: int) -> list[int]:
        statuses = []
        async with aiohttp.ClientSession() as session:
            for headers in (
                {**upgrade_headers(), **creds("0" * 43)},  # a token nobody holds
                {**upgrade_headers(), **creds(w.token, new_fingerprints(1)[0])},  # the token on another certificate
                upgrade_headers(),  # no credentials
            ):
                with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                    await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=headers)
                statuses.append(refused.value.status)
        return statuses

    assert all(status in {401, 403} for status in asyncio.run(serve_and(adapter, w, scenario)))
    assert adapter.handed_on == [] and w.frames == []


def test_a_revoked_device_cannot_reconnect_and_nothing_is_handed_on(module, tmp_path):
    adapter, w = rig(module, tmp_path)
    w.revoked = True

    async def scenario(port: int) -> int:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            return refused.value.status

    assert asyncio.run(serve_and(adapter, w, scenario)) == 403 and adapter.handed_on == []


def test_a_device_revoked_after_its_first_message_gets_no_second_one_handed_on(module, tmp_path):
    adapter, w = rig(module, tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> tuple[WSMsgType, int | None]:
        await ws.send_str(msg("work"))
        await until_pong(ws)
        w.revoked = True
        await ws.send_str(msg("work", SECOND_ID))
        message = await ws.receive()
        return message.type, ws.close_code

    assert run(adapter, w, scenario) == (WSMsgType.CLOSE, 4403)
    assert [event.message_id for event in adapter.handed_on] == [MSG_IN["id"]]


def test_a_refusal_reaches_only_the_device_whose_frame_it_answers(module, tmp_path):
    adapter, w = rig(module, tmp_path)
    other_fp = new_fingerprints(1)[0]
    _, other_token = w.registry.add("other", other_fp, list(LANES))

    async def scenario(port: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async with aiohttp.ClientSession() as session:
            first = await hello(session, port, w.headers())
            second = await hello(session, port, {**upgrade_headers(), **creds(other_token, other_fp)})
            await first.send_str(msg("other"))  # a lane the config does not know: lane_forbidden
            mine = await until_pong(first)
            await second.send_str(json.dumps(PING))
            theirs = [await second.receive_json()]
            await first.close()
            await second.close()
            return mine, theirs

    mine, theirs = asyncio.run(serve_and(adapter, w, scenario))
    assert [f["type"] for f in mine] == ["error", "pong"] and mine[0]["body"]["code"] == "lane_forbidden"
    assert [f["type"] for f in theirs] == ["pong"] and adapter.handed_on == []


def test_a_failing_hand_on_ends_the_session_without_the_text_and_the_re_send_is_handed_on(
    module, tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    adapter, w = rig(module, tmp_path)
    fail = {"on": True}

    async def handle(self: Any, event: Any) -> None:
        if fail["on"]:
            raise RuntimeError(f"handle_message failed for {event.text}")  # a library error that quotes the text
        self.handed_on.append(event)

    monkeypatch.setattr(FakeBaseAdapter, "handle_message", handle)

    async def first_try(ws: aiohttp.ClientWebSocketResponse) -> tuple[WSMsgType, int | None]:
        await ws.send_str(msg("work"))
        message = await ws.receive()
        return message.type, ws.close_code

    assert run(adapter, w, first_try) == (WSMsgType.CLOSE, 1011)
    assert TEXT not in caplog.text and "session failed (RuntimeError)" in caplog.text
    fail["on"] = False

    async def re_send(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        await ws.send_str(msg("work"))  # the same envelope id after reconnecting
        await ws.send_str(msg("work"))  # and a third copy
        return await until_pong(ws)

    assert [f["type"] for f in run(adapter, w, re_send)] == ["pong"]
    assert [event.message_id for event in adapter.handed_on] == [MSG_IN["id"]]


def test_a_stalled_gate_check_bounds_the_wait_drops_the_message_and_the_loop_reads_on(module, tmp_path, monkeypatch):
    adapter, w = rig(module, tmp_path)
    monkeypatch.setattr(module.gate, "LOCK_TIMEOUT", 0.1)  # the adapter waits LOCK_TIMEOUT + 1 s for a check

    def stalled() -> list[str]:
        time.sleep(1.6)  # a recompute that blocks its worker thread
        return []

    monkeypatch.setattr(adapter._gate_cache, "violations", stalled)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> tuple[float, list[str]]:
        start = time.monotonic()
        await ws.send_str(msg("work"))
        frames = await until_pong(ws)
        return time.monotonic() - start, [f["type"] for f in frames]

    elapsed, types = run(adapter, w, scenario)
    assert types == ["pong"] and adapter.handed_on == [] and elapsed < 1.5, (elapsed, types)


def test_an_error_frame_is_not_sent_and_nothing_raises_while_the_router_is_not_bound(module, tmp_path):
    adapter = new_adapter(module, {"lanes": ["work"], "home_lane": "work"})
    device = SimpleNamespace(device_id="dev-a", lanes=("work",))
    frame = json.loads(msg("other"))
    asyncio.run(adapter.router.on_frame(device, frame))  # lane_forbidden: no table, no schema yet
    assert adapter.handed_on == []


def test_an_error_for_a_device_without_a_connection_is_dropped_quietly(module, tmp_path):
    adapter, w = rig(module, tmp_path)
    app = build_app(w.deps())
    adapter.router.bind(app[CONNECTIONS], SCHEMA)  # a table nobody is connected to
    asyncio.run(adapter.router.on_frame(w.device, json.loads(msg("other"))))
    assert adapter.handed_on == []


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(True, True), (False, False), ("yes", False), (1, False), (None, False), (object(), False)],
    ids=["true", "false", "truthy string", "one", "none", "object"],
)
def test_only_the_gateways_own_true_marks_a_route_as_rejected(router_module, flag, expected):  # noqa: F811 - fixture
    source = SimpleNamespace(profile_route_rejected=flag)
    assert router_module._unrouted(source) is expected
    assert router_module._unrouted(SimpleNamespace()) is False  # a source without the mark
