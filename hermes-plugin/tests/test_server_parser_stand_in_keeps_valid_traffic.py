# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.239): the parser stand-in of ``_Handler`` must not change what a valid request does, and a failure of
the start-up probe that builds one handler must leave nothing set up. Valid traffic goes to the production runner
twice, once with the stand-in and once with it replaced by the parser itself, and the raw replies must be equal; the
same cases run again in a child process with ``AIOHTTP_NO_EXTENSIONS`` set (the pure-Python parser)."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web, web_protocol
from aiohttp.http_exceptions import BadStatusLine
from aiohttp.http_parser import HttpRequestParser
from server_helpers import HELLO, app_for, serving, wiring

from glasses import server_runner
from glasses.server import AiohttpDrift, build_runner

PURE_PYTHON = bool(os.environ.get("AIOHTTP_NO_EXTENSIONS"))
BODY = b"b" * 200_000
CLOSE = b"Connection: close\r\n"


def reply_of(raw: bytes) -> bytes:
    return re.sub(rb"Date: [^\r]*", b"Date: X", raw)


VALID = {
    "404": b"GET /nothing HTTP/1.1\r\nHost: x\r\n" + CLOSE + b"\r\n",
    "401": b"GET /ws HTTP/1.1\r\nHost: x\r\n" + CLOSE + b"\r\n",
    "405": b"DELETE /ws HTTP/1.1\r\nHost: x\r\n" + CLOSE + b"\r\n",
    "keep-alive-then-close": b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\nGET /nothing HTTP/1.1\r\nHost: x\r\n"
    + CLOSE
    + b"\r\n",
    "http-1-0": b"GET /nothing HTTP/1.0\r\n\r\n",
    "head": b"HEAD /nothing HTTP/1.1\r\nHost: x\r\n" + CLOSE + b"\r\n",
    "absolute-form": b"GET http://example.com/nothing HTTP/1.1\r\nHost: x\r\n" + CLOSE + b"\r\n",
    "absolute-form-ipv6": b"GET http://[::1]:80/nothing HTTP/1.1\r\nHost: x\r\n" + CLOSE + b"\r\n",
    "many-headers": b"GET /nothing HTTP/1.1\r\nHost: x\r\n"
    + b"".join(b"X-%d: v\r\n" % i for i in range(100))
    + CLOSE
    + b"\r\n",
    "large-body": b"POST /echo HTTP/1.1\r\nHost: x\r\n" + CLOSE + b"Content-Length: %d\r\n\r\n" % len(BODY) + BODY,
    "chunked": b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
    + CLOSE
    + b"\r\n5\r\nhello\r\n6;a=b\r\n world\r\n0\r\nX-T: 1\r\n\r\n",
    "expect-continue": b"POST /echo HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: 3\r\n"
    + CLOSE
    + b"\r\nabc",
    "pipelined-post-get": b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc"
    + b"GET /nothing HTTP/1.1\r\nHost: x\r\n"
    + CLOSE
    + b"\r\n",
}


async def echo(request: web.Request) -> web.Response:
    return web.Response(text=f"len={len(await request.read())}")


async def send(port: int, data: bytes, pieces: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    step = max(1, len(data) // pieces)
    for start in range(0, len(data), step):  # split writes: the parser gets the request in several feed_data calls
        writer.write(data[start : start + step])
        await writer.drain()
    try:
        return reply_of(await asyncio.wait_for(reader.read(), 10))
    finally:
        writer.close()


async def valid_traffic(tmp_path: Path) -> dict[str, Any]:
    tmp_path.mkdir()
    w = wiring(tmp_path)
    app = app_for(w)
    app.router.add_post("/echo", echo)
    seen: dict[str, Any] = {}
    async with serving(app) as port:
        for name, data in VALID.items():
            seen[name] = [await send(port, data, pieces) for pieces in (1, 9)]
        async with aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await ws.send_str(json.dumps(HELLO))
            seen["ws-welcome"] = (await ws.receive_json())["type"]
            await ws.close()
            seen["ws-close-code"] = ws.close_code
        headers = "".join(f"{k}: {v}\r\n" for k, v in w.headers().items())
        upgrade = f"GET /ws HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(upgrade)
        seen["upgrade-101"] = await asyncio.wait_for(reader.readline(), 10)  # the socket stays open: read one line
        writer.close()
    return seen


def test_valid_requests_get_the_same_replies_with_and_without_the_parser_stand_in(tmp_path: Path, monkeypatch):
    with_stand_in = asyncio.run(asyncio.wait_for(valid_traffic(tmp_path / "a"), 90))
    monkeypatch.setattr(server_runner, "_TypeOnlyParser", lambda parser: parser)
    without = asyncio.run(asyncio.wait_for(valid_traffic(tmp_path / "b"), 90))
    assert with_stand_in == without
    assert with_stand_in["404"][0].startswith(b"HTTP/1.1 404") and with_stand_in["401"][0].startswith(b"HTTP/1.1 401")
    assert with_stand_in["chunked"][0].endswith(b"len=11") and with_stand_in["large-body"][0].endswith(b"len=200000")
    assert with_stand_in["http-1-0"][0].startswith(b"HTTP/1.0 404")
    assert with_stand_in["ws-welcome"] == "welcome" and with_stand_in["ws-close-code"] == 1000
    assert with_stand_in["upgrade-101"].startswith(b"HTTP/1.1 101")


def test_the_same_valid_traffic_passes_with_the_pure_python_parser(tmp_path: Path):
    if PURE_PYTHON:
        pytest.skip("this is the child run")
    child = ["-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={tmp_path / 'child'}", __file__]
    done = subprocess.run(
        [sys.executable, *child, "-k", "same_replies"],
        env={**os.environ, "AIOHTTP_NO_EXTENSIONS": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-3000:]
    assert "1 passed" in done.stdout, done.stdout[-500:]


def real_parser() -> Any:
    class Protocol:  # what the parser needs of its protocol: nothing it calls while parsing a head
        _reading_paused = False

    loop = asyncio.new_event_loop()
    try:
        return HttpRequestParser(Protocol(), loop, 2**16, max_line_size=8190, max_field_size=8190)
    finally:
        loop.close()


def test_the_stand_in_hands_back_the_parsers_tuple_and_delegates_every_other_attribute():
    parser = real_parser()
    stand_in = server_runner._TypeOnlyParser(parser)
    messages, upgraded, tail = stand_in.feed_data(
        b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\nTAIL"
    )
    assert (len(messages), upgraded, tail) == (1, True, b"TAIL")
    for name in ("set_upgraded", "pause_reading", "message_consumed", "feed_eof"):
        assert getattr(stand_in, name) == getattr(parser, name)
    stand_in.set_upgraded(False)
    assert stand_in.feed_data(b"GET /x HTTP/1.1\r\nHost: x\r\n\r\n")[1] is False
    with pytest.raises(AttributeError):
        stand_in.no_such_attribute  # noqa: B018 - the attribute access itself is what raises


class Failing:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def feed_data(self, data: bytes) -> None:
        raise self.exc


@pytest.mark.parametrize("exc", [RuntimeError("bug"), KeyError("k"), TypeError("t"), AssertionError("a")])
def test_the_stand_in_lets_an_error_that_is_not_a_value_error_through_unchanged(exc):
    with pytest.raises(type(exc)) as caught:
        server_runner._TypeOnlyParser(Failing(exc)).feed_data(b"x")
    assert caught.value is exc


def test_the_stand_in_lets_a_parser_refusal_through_with_its_own_type_and_text():
    refusal = BadStatusLine("GET /x junk")  # the type is what the log line and the status of the answer follow
    with pytest.raises(BadStatusLine) as caught:
        server_runner._TypeOnlyParser(Failing(refusal)).feed_data(b"x")
    assert caught.value is refusal and type(caught.value) is BadStatusLine


@pytest.mark.xfail(
    strict=True, raises=RecursionError, reason="known gap: a stand-in built without __init__ recurses in __getattr__"
)
def test_a_stand_in_without_its_parser_raises_attribute_error_not_recursion():
    bare = object.__new__(server_runner._TypeOnlyParser)  # copy and pickle build instances this way
    with pytest.raises(AttributeError):
        bare.feed_eof  # noqa: B018 - the attribute access itself is what raises
    with pytest.raises(AttributeError):
        copy.copy(bare)


def test_a_handler_lets_go_of_the_stand_in_when_the_connection_is_lost():
    async def main() -> None:
        server = web.Server(lambda _r: web.Response())
        handler = server_runner._Handler(server, loop=asyncio.get_running_loop(), **server._kwargs)
        assert isinstance(handler._parser, server_runner._TypeOnlyParser)
        handler.connection_lost(None)
        assert handler._parser is None

    asyncio.run(main())


async def refused_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, breaking: Any, exc_type: type[Exception]
) -> dict[str, Any]:
    cleaned: list[str] = []

    async def on_cleanup(_app: web.Application) -> None:
        cleaned.append("app")

    app = app_for(wiring(tmp_path))
    app.on_cleanup.append(on_cleanup)
    runner = build_runner(app)
    monkeypatch.setattr(web_protocol, "HttpRequestParser", breaking)
    with pytest.raises(exc_type):
        await runner.setup()
    return {"cleaned": cleaned, "server": runner.server}


def test_a_parser_that_is_none_is_refused_by_setup_and_leaves_nothing_started(tmp_path: Path, monkeypatch):
    out = asyncio.run(refused_setup(tmp_path, monkeypatch, lambda *a, **k: None, AiohttpDrift))
    assert out == {"cleaned": ["app"], "server": None}


def test_a_parser_whose_feed_data_is_not_callable_is_refused_by_setup(tmp_path: Path, monkeypatch):
    class Odd:
        feed_data = 3

    out = asyncio.run(refused_setup(tmp_path, monkeypatch, lambda *a, **k: Odd(), AiohttpDrift))
    assert out == {"cleaned": ["app"], "server": None}


def broken_parser(*args: Any, **kwargs: Any) -> Any:
    raise TypeError("HttpRequestParser() got an unexpected keyword argument")  # the parser's signature drifted


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="known gap: a probe failure other than AiohttpDrift leaves the runner set up",
)
def test_a_handler_that_cannot_be_built_leaves_nothing_started(tmp_path: Path, monkeypatch):
    out = asyncio.run(refused_setup(tmp_path, monkeypatch, broken_parser, TypeError))
    assert out == {"cleaned": ["app"], "server": None}
