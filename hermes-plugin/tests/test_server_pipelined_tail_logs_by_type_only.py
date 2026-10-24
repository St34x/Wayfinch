# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.239): a request tail pipelined behind a refused upgrade is parsed again by aiohttp in
``finish_response``, outside ``handle_error``. A parser error found there must reach no log record of any logger with
the offending bytes (they may hold a ``Bearer`` token) or a traceback: it is logged by type only, the contract of
``handle_error``. Real loopback server through ``build_runner``; every logger is captured at DEBUG and every record is
rendered with a formatter, so ``exc_text`` counts. The client gets no answer behind such a tail (the 401 is lost by
design: no legitimate client pipelines behind an upgrade)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from server_log_helpers import (
    Capture,
    assert_quiet_and_alive,
    capturing,
    exchange,
    fake_token,
    upgrade_request,
)

from glasses.server_runner import _Handler


def malformed_line_tail(token: str) -> bytes:
    return f"GET /x?t={token} HTTP/1.1 junk\r\nHost: x\r\nAuthorization: Bearer {token}\r\n\r\n".encode()


def test_sat_l1_1_a_bad_byte_in_the_authorization_line_behind_a_401_upgrade_is_logged_by_type_only(tmp_path):
    token = fake_token()
    tail = f"GET /x HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\x00\r\n\r\n".encode()
    out = exchange(tmp_path, upgrade_request() + tail)
    assert_quiet_and_alive(out, token)


def test_sat_l1_2_the_token_in_a_malformed_request_line_behind_a_refused_upgrade_is_logged_by_type_only(tmp_path):
    token = fake_token()
    out = exchange(tmp_path, upgrade_request() + malformed_line_tail(token))
    assert_quiet_and_alive(out, token)


def test_sat_l1_3_the_no_certificate_401_with_a_malformed_tail_is_logged_by_type_only(tmp_path):
    token = fake_token()
    out = exchange(tmp_path, upgrade_request() + malformed_line_tail(token), client_fingerprint=lambda _r: None)
    assert_quiet_and_alive(out, token)


def test_sat_l1_4_control_a_line_with_no_colon_behind_a_refused_upgrade_leaves_no_error_and_no_traceback(tmp_path):
    token = fake_token()
    tail = f"GET /x HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\nno colon here\r\n\r\n".encode()
    out = exchange(tmp_path, upgrade_request() + tail)
    assert_quiet_and_alive(out, token)


def test_sat_l1_5_an_exception_that_is_not_a_parser_error_still_gets_aiohttps_error_record(tmp_path):
    out = exchange(tmp_path, b"GET /boom HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert out.reply.startswith(b"HTTP/1.1 500"), out.reply[:80]
    assert out.log.errors("aiohttp.server") == ["Error handling request from 127.0.0.1"]
    assert any("RuntimeError: handler failed" in text for text in out.log.rendered)  # with its traceback


def parser_error() -> BadStatusLine:
    return BadStatusLine("GET /x?t=SECRET-QUOTED-LINE HTTP/1.1 junk")


def call_log_exception(*args: Any, **kw: Any) -> Capture:
    """``_Handler.log_exception`` as aiohttp calls it, on a real handler instance and outside any ``except`` block."""

    async def main() -> None:
        server = web.Server(lambda _r: web.Response())
        _Handler(server, loop=asyncio.get_running_loop(), **server._kwargs).log_exception(*args, **kw)

    with capturing() as capture:
        asyncio.run(main())
    return capture


def test_a_parser_error_in_exc_info_is_logged_at_debug_by_type_name_only():
    log = call_log_exception("Unhandled exception", exc_info=parser_error())  # no exception is being handled here
    mine = log.of("aiohttp.server")
    assert [r.levelno for r in mine] == [logging.DEBUG] and mine[0].exc_info is None
    assert "BadStatusLine" in mine[0].getMessage() and "SECRET-QUOTED-LINE" not in "".join(log.rendered)


def test_exc_info_true_falls_back_to_the_exception_being_handled():
    try:
        raise parser_error()
    except BadStatusLine:  # aiohttp's logger.exception sets exc_info=True from inside an except block
        log = call_log_exception("Unhandled exception", exc_info=True)
    assert [r.levelno for r in log.of("aiohttp.server")] == [logging.DEBUG]
    assert "SECRET-QUOTED-LINE" not in "".join(log.rendered)


def test_any_other_exception_goes_to_aiohttps_log_exception_unchanged():
    log = call_log_exception("Unhandled exception", exc_info=RuntimeError("real error"))
    mine = log.of("aiohttp.server")
    assert [(r.levelno, r.getMessage()) for r in mine] == [(logging.ERROR, "Unhandled exception")]
    assert "RuntimeError: real error" in "".join(log.rendered)


def test_a_call_without_exc_info_goes_to_aiohttps_log_exception_unchanged():
    log = call_log_exception("Missing return statement on request handler")
    mine = log.of("aiohttp.server")
    assert [(r.levelno, r.getMessage()) for r in mine] == [
        (logging.ERROR, "Missing return statement on request handler")
    ]
