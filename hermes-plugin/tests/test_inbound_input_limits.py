# SPDX-License-Identifier: Apache-2.0
"""What a ``msg.in`` may carry to reach the agent (T1.08): the text arrives byte for byte up to the protocol limits
(2000 code points, 2048 UTF-8 bytes), and every malformed or oversized frame is refused by the codec before the
inbound path, with a fixed ``error`` frame and without the text, the loop reading on. Through the real WS server and
the adapter's frame router, as in ``test_inbound_session.py`` (stand-in Hermes base class)."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType
from server_helpers import MSG_IN, SCHEMA

from test_adapter import adapter_module  # noqa: F401 - the fixture is used by name
from test_inbound_session import rig, run, until_pong


@pytest.fixture
def module(adapter_module):  # noqa: F811 - the imported fixture
    return adapter_module


TEXT = "planted-text-4471"
IDS = [f"01JABCDEFGHJKMNPQRSTVWX{n:03d}" for n in range(40)]


def frame_with(text: str = TEXT, frame_id: str = MSG_IN["id"], **changes: Any) -> dict[str, Any]:
    frame = copy.deepcopy(MSG_IN)
    frame["id"], frame["body"]["text"] = frame_id, text
    frame["lane"] = "work"
    for key, value in changes.items():
        if key.startswith("body_"):
            frame["body"][key[5:]] = value
        else:
            frame[key] = value
    return frame


def send_all(adapter: Any, w: Any, frames: list[str]) -> list[dict[str, Any]]:
    """Send *frames* in order, then a ping; every frame the server sent back up to the pong."""

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        for raw in frames:
            await ws.send_str(raw)
        return await until_pong(ws)

    return run(adapter, w, scenario)


@pytest.mark.parametrize(
    "text",
    [
        "x" * 2000,  # the code-point limit
        "\U0001f600" * 500,  # 2000 UTF-8 bytes: the byte limit, 500 code points
        "\u00e9" * 1000,  # 2000 UTF-8 bytes of two-byte chars
        "  leading and trailing whitespace \n\n",
        "line one\nline two\r\n\ttabbed",
        "\x00 a NUL and \x1b[31m an escape \x7f",
        "\u202e right-to-left override \u200b zero width \ufeff",
        "/approve always",
        "   ",  # the schema asks for one character, not one visible character
    ],
    ids=[
        "2000 chars",
        "2000 bytes",
        "2000 bytes 2-byte",
        "whitespace",
        "newlines",
        "control chars",
        "format chars",
        "slash",
        "blanks",
    ],
)
def test_the_text_reaches_the_event_byte_for_byte_up_to_the_limits(module, tmp_path, text):
    adapter, w = rig(module, tmp_path)
    assert SCHEMA.is_valid(frame_with(text))
    assert [f["type"] for f in send_all(adapter, w, [json.dumps(frame_with(text))])] == ["pong"]
    (event,) = adapter.handed_on
    assert event.text == text and len(event.text.encode()) == len(text.encode())


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("x" * 2001, "invalid_frame"),
        ("\U0001f600" * 501, "field_too_long"),
        ("\u00e9" * 1001, "field_too_long"),
        ("", "invalid_frame"),
    ],
    ids=["2001 chars", "2004 bytes", "2002 bytes of two-byte chars", "empty"],
)
def test_text_over_the_limits_or_empty_is_refused_by_the_codec_and_never_handed_on(
    module, tmp_path, text, code, caplog
):
    caplog.set_level(logging.DEBUG)
    adapter, w = rig(module, tmp_path)
    marker = "\u00e9" if text.startswith("\u00e9") else (text[:1] or "x")
    frames = send_all(adapter, w, [json.dumps(frame_with(text))])
    error, pong = frames
    assert error["type"] == "error" and error["body"]["code"] == code and "corr" not in error
    assert "seq" not in error and SCHEMA.is_valid(error) and pong["type"] == "pong"
    assert adapter.handed_on == []
    assert marker * 50 not in json.dumps(frames) and marker * 50 not in caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"body_origin": "dictated"},
        {"body_origin": None},
        {"body_text": 7},
        {"body_text": ["a"]},
        {"body_text": None},
        {"body_extra": "x"},
        {"body_userId": "dev-forged"},
        {"lane": "Work"},
        {"lane": "work\nadmin"},
        {"lane": "glasses:work"},
        {"lane": "../work"},
        {"lane": "w" * 33},
        {"lane": ""},
        {"lane": None},
        {"lane": 5},
        {"lane": ["work"]},
        {"seq": 4},
        {"corr": "not-a-ulid"},
        {"id": "short"},
        {"v": 2},
        {"type": "msg.in ", "body": {"text": TEXT, "origin": "ptt"}},
    ],
    ids=lambda changes: next(iter(changes)) + "=" + json.dumps(next(iter(changes.values())), default=str)[:20],
)
def test_a_malformed_msg_in_is_an_error_frame_and_the_loop_keeps_reading(module, tmp_path, changes, caplog):
    caplog.set_level(logging.DEBUG)
    adapter, w = rig(module, tmp_path)
    bad = json.dumps(frame_with(**changes))
    good = json.dumps(frame_with(frame_id=IDS[1]))
    frames = send_all(adapter, w, [bad, good])
    assert [f["type"] for f in frames] == ["error", "pong"], frames
    assert frames[0]["body"]["code"] in {"invalid_frame", "unknown_type"} and "seq" not in frames[0]
    assert [e.message_id for e in adapter.handed_on] == [IDS[1]]  # only the good frame; the bad one took no id
    assert TEXT not in json.dumps(frames) and TEXT not in caplog.text


def test_a_refused_frame_takes_no_dedupe_entry_so_its_corrected_re_send_is_handed_on(module, tmp_path):
    adapter, w = rig(module, tmp_path)
    frames = send_all(adapter, w, [json.dumps(frame_with(body_origin="dictated")), json.dumps(frame_with())])
    assert [f["type"] for f in frames] == ["error", "pong"] and len(adapter.handed_on) == 1


def test_a_frame_over_the_size_cap_closes_the_connection_and_is_never_handed_on(module, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    adapter, w = rig(module, tmp_path)
    big = json.dumps(frame_with("x" * 70_000))

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> tuple[WSMsgType, int | None]:
        await ws.send_str(big)
        seen = await ws.receive()
        while seen.type not in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
            seen = await ws.receive()
        return seen.type, ws.close_code

    kind, code = run(adapter, w, scenario)
    assert kind in {WSMsgType.CLOSE, WSMsgType.CLOSED} and code in {1009, 1008}
    assert adapter.handed_on == [] and "x" * 50 not in caplog.text


def test_a_flood_of_msg_in_with_distinct_ids_is_handed_on_in_order_without_a_frame(module, tmp_path):
    adapter, w = rig(module, tmp_path)
    frames = send_all(adapter, w, [json.dumps(frame_with(f"t{n}", IDS[n])) for n in range(len(IDS))])
    assert [f["type"] for f in frames] == ["pong"]
    assert [e.message_id for e in adapter.handed_on] == IDS and [e.text for e in adapter.handed_on][:2] == ["t0", "t1"]


def test_truncated_json_and_invalid_utf8_never_reach_the_hand_on_and_the_loop_keeps_reading(module, tmp_path):
    adapter, w = rig(module, tmp_path)

    async def scenario(ws: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
        await ws.send_str('{"v":1,"id":"' + IDS[0] + '","type":"msg.in","body":')
        await ws.send_bytes(json.dumps(frame_with(IDS[0])).encode()[:-5] + b"\xff\xfe")  # invalid UTF-8
        await ws.send_str(json.dumps(frame_with(frame_id=IDS[2])))
        return await until_pong(ws)

    frames = run(adapter, w, scenario)
    assert [f["type"] for f in frames] == ["error", "error", "pong"]
    assert [f["body"]["code"] for f in frames[:2]] == ["invalid_json", "invalid_text"]
    assert [e.message_id for e in adapter.handed_on] == [IDS[2]]
