# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.239): the refusals of the request parser in more shapes and places than the first tests (a target yarl
cannot build, an oversize line, a bad method, a bad chunk size, a bad byte in the ``Authorization`` line), as a first
request, after a valid request on the same connection, and behind an upgrade refused for three reasons; many at once;
and a ``ValueError`` that is not a parse error. Real loopback server through ``build_runner``; every logger captured at
DEBUG; the cases run again in a child process with ``AIOHTTP_NO_EXTENSIONS`` set (the pure-Python parser)."""

from __future__ import annotations

import asyncio
import gc
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp.http_exceptions import BadHttpMessage
from server_helpers import app_for, serving, wiring
from server_log_helpers import FRESH_REQUEST, assert_quiet_and_alive, capturing, exchange, fake_token, upgrade_request

from glasses.server import _CAP
from glasses.server_runner import _TypeOnlyParser

PURE_PYTHON = bool(os.environ.get("AIOHTTP_NO_EXTENSIONS"))
HOST = b"Host: x\r\n"
VALID = b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n"
NO_CERTIFICATE = {"client_fingerprint": lambda _r: None}

PAYLOADS: dict[str, Callable[[str], bytes]] = {
    "ipv6-with-userinfo": lambda t: f"GET http://u:{t}@[::1/ HTTP/1.1\r\n".encode() + HOST + b"\r\n",
    "oversize-header": lambda t: b"GET /x HTTP/1.1\r\n" + HOST + f"X-{t}: ".encode() + b"A" * 70000 + b"\r\n\r\n",
    "oversize-target": lambda t: f"GET /{t}".encode() + b"a" * 70000 + b" HTTP/1.1\r\n" + HOST + b"\r\n",
    "invalid-method": lambda t: f"G@T /{t} HTTP/1.1\r\n".encode() + HOST + b"\r\n",
    "bad-chunk-size": lambda t: (
        b"POST /boom HTTP/1.1\r\n" + HOST + b"Transfer-Encoding: chunked\r\n\r\n" + f"ZZ{t}\r\n".encode()
    ),
    "bad-byte-in-authorization": lambda t: (
        b"GET /x HTTP/1.1\r\n" + HOST + f"Authorization: Bearer {t}\x01\r\n\r\n".encode()
    ),
    "userinfo-in-fragment": lambda t: f"GET http://u:{t}@h/#a b HTTP/1.1\r\n".encode() + HOST + b"\r\n",
}
PAYLOAD = pytest.mark.parametrize("name", list(PAYLOADS))


def assert_refused_like_any_parser_error(out, token: str, answered: bool) -> None:
    assert_quiet_and_alive(out, token)
    if answered:
        assert b" 400 Bad Request\r\n" in out.reply, out.reply[:120]
        assert token.encode() not in out.reply and out.reply.rstrip().endswith(b"Bad Request")
    else:
        assert out.reply == b"", out.reply[:80]  # the lost 401: the tail ends the connection unanswered


@PAYLOAD
def test_a_refused_first_request_is_answered_400_and_logged_by_type_only(tmp_path, name):
    token = fake_token()
    assert_refused_like_any_parser_error(exchange(tmp_path, PAYLOADS[name](token)), token, answered=True)


@PAYLOAD
def test_a_refused_request_after_a_valid_one_on_the_same_connection_is_answered_400(tmp_path, name):
    token = fake_token()
    assert_refused_like_any_parser_error(exchange(tmp_path, VALID + PAYLOADS[name](token)), token, answered=True)


@PAYLOAD
def test_a_refused_tail_behind_a_wrong_token_401_is_logged_by_type_only(tmp_path, name):
    token = fake_token()
    assert_refused_like_any_parser_error(exchange(tmp_path, upgrade_request() + PAYLOADS[name](token)), token, False)


@PAYLOAD
def test_a_refused_tail_behind_the_no_certificate_401_is_logged_by_type_only(tmp_path, name):
    token = fake_token()
    out = exchange(tmp_path, upgrade_request() + PAYLOADS[name](token), **NO_CERTIFICATE)
    assert_refused_like_any_parser_error(out, token, answered=False)


@PAYLOAD
def test_a_refused_tail_behind_an_upgrade_to_a_missing_path_is_logged_by_type_only(tmp_path, name):
    token = fake_token()
    upgrade = upgrade_request().replace(b"GET /ws ", b"GET /nothing ", 1)
    assert_refused_like_any_parser_error(exchange(tmp_path, upgrade + PAYLOADS[name](token)), token, answered=False)


def test_the_same_refusals_pass_with_the_pure_python_parser(tmp_path: Path):
    if PURE_PYTHON:
        pytest.skip("this is the child run")
    child = ["-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={tmp_path / 'child'}", __file__]
    done = subprocess.run(
        [sys.executable, *child, "-k", "not pure_python"],
        env={**os.environ, "AIOHTTP_NO_EXTENSIONS": "1"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-3000:]
    assert re.search(r"\b\d\d passed\b", done.stdout) and "skipped" not in done.stdout, done.stdout[-500:]


async def connect_and_read(port: int, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    try:
        return await asyncio.wait_for(reader.read(), 20)
    except ConnectionResetError:
        return b""
    finally:
        writer.close()


def test_many_connections_with_refused_requests_at_once_free_every_slot_and_never_stall_the_loop(tmp_path):
    tokens = [fake_token() for _ in range(120)]
    shapes = [
        (lambda t: PAYLOADS["ipv6-with-userinfo"](t), 400),
        (lambda t: upgrade_request() + PAYLOADS["ipv6-with-userinfo"](t), 0),
        (lambda t: upgrade_request() + PAYLOADS["bad-byte-in-authorization"](t), 0),
        (lambda t: PAYLOADS["invalid-method"](t), 400),
        (lambda t: FRESH_REQUEST, 404),
    ]
    gaps: list[float] = []

    async def main() -> tuple[list[bytes], dict[str, int], bytes]:
        done = False

        async def ticker() -> None:
            last = time.monotonic()
            while not done:
                await asyncio.sleep(0.005)
                gaps.append(time.monotonic() - last)
                last = time.monotonic()

        app = app_for(wiring(tmp_path))
        async with serving(app) as port:
            tick = asyncio.create_task(ticker())
            calls = [connect_and_read(port, shapes[i % len(shapes)][0](t)) for i, t in enumerate(tokens)]
            replies = list(await asyncio.gather(*calls))
            done = True
            await tick
            fresh = await connect_and_read(port, FRESH_REQUEST)
            return replies, dict(app[_CAP]._open), fresh

    with capturing() as log:
        replies, slots, fresh = asyncio.run(asyncio.wait_for(main(), 60))
        gc.collect()
    for i, reply in enumerate(replies):
        want = shapes[i % len(shapes)][1]
        assert (reply.split(b" ")[1:2] == [str(want).encode()]) if want else reply == b"", (i, reply[:60])
    assert slots == {} and fresh.startswith(b"HTTP/1.1 404")
    assert not [r for r in log.rendered if any(t in r for t in tokens) or "Traceback" in r]
    assert [r for r in log.records if r.levelno >= 40] == []
    assert max(gaps) < 1.0, f"the loop stalled for {max(gaps):.2f} s"  # measured about 0.08 s


class ValueErrorOutsideTheParser:
    """A parser whose own code fails with a ValueError that has nothing to do with the request's bytes."""

    def feed_data(self, data: bytes) -> None:
        raise ValueError("internal state of the parser is corrupt")


@pytest.mark.xfail(
    strict=True,
    raises=BadHttpMessage,
    reason="known gap: every ValueError of feed_data becomes a 400 logged by type only, a bug of aiohttp included",
)
def test_a_value_error_that_is_not_about_the_request_is_not_turned_into_a_parse_refusal():
    with pytest.raises(ValueError, match="internal state"):
        _TypeOnlyParser(ValueErrorOutsideTheParser()).feed_data(b"GET / HTTP/1.1\r\n\r\n")
