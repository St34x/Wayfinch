# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), what the production runner (``build_runner``) does with request bytes the HTTP parser refuses:
none of them (request line, header names, header values, a bad body line) reaches a response, a log record of any logger
or the process output; plus the checks that fail loudly when aiohttp changes the private parts the runner relies on
(``AppRunner._make_server``, ``Server.__call__``, ``RequestHandler.handle_error``) instead of quoting again."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re

import pytest
from aiohttp import web
from server_helpers import app_for, raw, serving, wiring

from glasses.server import build_runner
from glasses.server_runner import _Handler

SENT = "Qx7SentinelQx7SentinelQx7Sentinel"
HEAD = b"Host: x\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
S = SENT.encode()

REFUSED = {
    "nul-in-method": b"G\x00T /ws HTTP/1.1\r\n" + HEAD + b"Authorization: Bearer " + S + b"\r\n\r\n",
    "method-is-the-secret": S + b" /ws HTTP/1.1\r\n" + HEAD + b"\r\n",
    "version-9.9": b"GET /ws?t=" + S + b" HTTP/9.9\r\n" + HEAD + b"\r\n",
    "no-version": b"GET /ws\r\n" + HEAD + b"Authorization: Bearer " + S + b"\r\n\r\n",
    "spaces-in-target": b"GET /ws?t=" + S + b" x y HTTP/1.1\r\n" + HEAD + b"\r\n",
    "control-byte-in-target": b"GET /ws?t=" + S + b"\x01 HTTP/1.1\r\n" + HEAD + b"\r\n",
    "request-line-too-long": b"GET /" + b"A" * 9_000 + S + b" HTTP/1.1\r\n" + HEAD + b"\r\n",
    "header-name-with-space": b"GET /ws HTTP/1.1\r\n" + HEAD + b"X-Bad " + S + b": v\r\n\r\n",
    "space-before-colon": b"GET /ws HTTP/1.1\r\n" + HEAD + b"Authorization : Bearer " + S + b"\r\n\r\n",
    "header-line-without-colon": b"GET /ws HTTP/1.1\r\n" + HEAD + S + b"\r\n\r\n",
    "nul-in-value": b"GET /ws HTTP/1.1\r\n" + HEAD + b"X-Client-Cert: " + S + b"\x00\r\n\r\n",
    "high-bytes-in-value": b"GET /ws HTTP/1.1\r\n" + HEAD + b"Authorization: Bearer " + S + b"\xff\xfe\r\n\r\n",
    "lone-cr-in-value": b"GET /ws HTTP/1.1\r\n" + HEAD + b"Authorization: Bearer " + S + b"\rX: y\r\n\r\n",
    "bad-content-length": b"GET /ws HTTP/1.1\r\n" + HEAD + b"Content-Length: " + S + b"\r\n\r\n",
    "conflicting-content-lengths": b"POST /ws HTTP/1.1\r\n"
    + HEAD
    + b"Content-Length: 3\r\nContent-Length: 4\r\n\r\n"
    + S,
    "bad-transfer-encoding": b"POST /ws HTTP/1.1\r\n" + HEAD + b"Transfer-Encoding: " + S + b"\r\n\r\n",
    "bad-chunk-size-line": b"POST /ws HTTP/1.1\r\n" + HEAD + b"Transfer-Encoding: chunked\r\n\r\n" + S + b"\r\n",
    "too-many-headers": b"GET /ws HTTP/1.1\r\n"
    + HEAD
    + b"".join(b"X-%d: " % n + S + b"\r\n" for n in range(400))
    + b"\r\n",
    "tls-hello-like": b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + S + b"\r\n\r\n",
    "obs-fold-first-header": b"GET /ws HTTP/1.1\r\n " + S + b"\r\n" + HEAD + b"\r\n",
}


def refused_ids() -> list[str]:
    return sorted(REFUSED)


@pytest.mark.parametrize("case", refused_ids())
def test_a_request_the_parser_refuses_is_echoed_in_no_response_and_no_log_or_output(tmp_path, caplog, capfd, case):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)

    async def main() -> bytes:
        async with serving(app_for(w)) as port:
            return await raw(port, REFUSED[case])

    answer = asyncio.run(asyncio.wait_for(main(), 20))
    status = answer.split(b"\r\n", 1)[0]
    assert answer == b"" or re.match(rb"HTTP/\d\.\d 4\d\d ", status), (
        status
    )  # a refusal or a 401: never 101, 200 or 5xx
    logged = caplog.text + "".join(f"{r.getMessage()}{r.args}{r.exc_text}{r.exc_info}" for r in caplog.records)
    output = "".join(capfd.readouterr())
    assert S not in answer and SENT not in logged and SENT not in output
    assert "Traceback" not in logged and "Traceback" not in output
    if status.split(b" ")[1:2] == [b"400"]:
        assert answer.split(b"\r\n\r\n", 1)[1] == b"Bad Request"  # the status phrase, nothing of the line


def test_the_control_with_aiohttps_own_runner_does_echo_the_line_so_the_cases_above_are_not_vacuous(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    line = b"GET /ws HTTP/1.1\r\n" + HEAD + b"Authorization: Bearer " + S + b"\x01\r\n\r\n"

    async def main() -> bytes:
        runner = web.AppRunner(app_for(w))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            return await raw(int(runner.addresses[0][1]), line)
        finally:
            await runner.cleanup()

    answer = asyncio.run(asyncio.wait_for(main(), 20))
    if S not in answer and SENT not in caplog.text:
        pytest.skip(
            "aiohttp no longer echoes a refused line: the runner's handler may be dropped (revisit build_runner)"
        )
    assert S in answer or SENT in caplog.text


def test_the_runner_builds_connection_handlers_of_the_quote_free_class(tmp_path):
    async def main() -> object:
        runner = build_runner(app_for(wiring(tmp_path)))
        await runner.setup()
        try:
            assert runner.server is not None
            return runner.server()  # what aiohttp calls for every accepted connection
        finally:
            await runner.cleanup()

    handler = asyncio.run(main())
    assert isinstance(handler, _Handler) and isinstance(handler, web.RequestHandler)
    assert type(handler).handle_error is not web.RequestHandler.handle_error


def test_the_aiohttp_internals_the_runner_replaces_still_have_the_shape_it_relies_on(tmp_path):
    """If aiohttp renames or re-signs any of these, this fails first and names the part."""
    assert inspect.iscoroutinefunction(web.AppRunner._make_server)  # _Runner awaits and overrides it
    handle = list(inspect.signature(web.RequestHandler.handle_error).parameters)
    mine = list(inspect.signature(_Handler.handle_error).parameters)
    assert mine == handle == ["self", "request", "status", "exc", "message"]
    assert list(inspect.signature(web.Server.__call__).parameters) == ["self"]

    async def main() -> tuple[bool, bool]:
        runner = build_runner(app_for(wiring(tmp_path)))
        await runner.setup()
        try:
            assert runner.server is not None
            return hasattr(runner.server, "_kwargs"), hasattr(runner.server, "_loop")
        finally:
            await runner.cleanup()

    assert asyncio.run(main()) == (True, True)


def test_a_credential_in_the_query_string_of_a_well_formed_request_is_not_in_the_access_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    line = b"GET /ws?token=" + S + b" HTTP/1.1\r\n" + HEAD + b"\r\n"

    async def main() -> bytes:
        async with serving(app_for(w)) as port:
            return await raw(port, line)

    answer = asyncio.run(asyncio.wait_for(main(), 20))
    assert answer.startswith(b"HTTP/1.1 401 ")
    assert SENT not in caplog.text
    access = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert access and re.search(r'"GET /ws" 401 \d+$', access[-1]), access  # method, path, status, integer ms


def test_a_path_with_an_encoded_line_break_stays_one_encoded_access_log_line(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    w = wiring(tmp_path)

    async def main() -> bytes:
        async with serving(app_for(w)) as port:
            return await raw(port, b"GET /ws%0d%0aFORGED%20LINE HTTP/1.1\r\n" + HEAD + b"\r\n")

    asyncio.run(asyncio.wait_for(main(), 20))
    access = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert len(access) == 1 and "\n" not in access[0] and "\r" not in access[0]
    assert "%0d%0aFORGED%20LINE" in access[0]
