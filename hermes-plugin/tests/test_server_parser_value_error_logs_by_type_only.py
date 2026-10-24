# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.239): the request parser raises more than ``HttpProcessingError``. An absolute-form request target
makes yarl raise a plain ``ValueError`` from inside ``feed_data`` (with the pure-Python parser its message quotes the
netloc). Such a refusal, in the tail pipelined behind a refused upgrade and in a first request, must reach no log record
of any logger with the offending bytes or a traceback: it is logged by type only and ends like any refused request (a
first request is answered ``400 Bad Request``). Real loopback server through ``build_runner``; the cases run with the
default parser and again in a child process with ``AIOHTTP_NO_EXTENSIONS`` set, the choice aiohttp makes at import."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp.http_exceptions import BadHttpMessage
from aiohttp.http_parser import HttpRequestParser, HttpRequestParserPy
from server_log_helpers import assert_quiet_and_alive, exchange, fake_token, upgrade_request

PURE_PYTHON = bool(os.environ.get("AIOHTTP_NO_EXTENSIONS"))


def ipv6_target(token: str) -> bytes:
    return f"GET http://[{token}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode()


def nfkc_netloc_target(token: str) -> bytes:  # U+2100 changes under NFKC normalisation: yarl quotes the netloc
    return f"GET http://u:{token}@a\u2100b/ HTTP/1.1\r\nHost: x\r\n\r\n".encode()


TARGETS = pytest.mark.parametrize(
    "target",
    [
        pytest.param(ipv6_target, id="ipv6"),
        pytest.param(
            nfkc_netloc_target,
            id="nfkc_netloc",
            marks=pytest.mark.skipif(not PURE_PYTHON, reason="the C parser builds this target without an error"),
        ),
    ],
)


def test_the_request_parser_is_the_one_the_environment_selects():
    assert (HttpRequestParser is HttpRequestParserPy) == PURE_PYTHON


@TARGETS
def test_a_target_the_parser_cannot_build_behind_a_401_upgrade_is_logged_by_type_only(tmp_path, target):
    token = fake_token()
    assert_quiet_and_alive(exchange(tmp_path, upgrade_request() + target(token)), token)


@TARGETS
def test_a_target_the_parser_cannot_build_behind_the_no_certificate_401_is_logged_by_type_only(tmp_path, target):
    token = fake_token()
    out = exchange(tmp_path, upgrade_request() + target(token), client_fingerprint=lambda _r: None)
    assert_quiet_and_alive(out, token)


@TARGETS
def test_a_first_request_the_parser_cannot_build_is_answered_400_and_logged_by_type_only(tmp_path, target):
    token = fake_token()
    out = exchange(tmp_path, target(token))
    assert_quiet_and_alive(out, token)
    assert out.reply.split(b"\r\n")[0].endswith(b" 400 Bad Request"), out.reply[:80]  # aiohttp answers HTTP/1.0 here
    assert token.encode() not in out.reply and out.reply.rstrip().endswith(b"Bad Request")


def test_the_same_cases_pass_with_the_pure_python_parser(tmp_path: Path):
    if PURE_PYTHON:
        pytest.skip("this is the child run")
    child = ["-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={tmp_path / 'child'}", __file__]
    done = subprocess.run(
        [sys.executable, *child, "-k", "not pure_python"],
        env={**os.environ, "AIOHTTP_NO_EXTENSIONS": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-3000:]
    assert " passed" in done.stdout and "skipped" not in done.stdout, done.stdout[-500:]  # the child skips nothing


class QuotingParser:
    def __init__(self, text: str) -> None:
        self.text = text

    def feed_data(self, data: bytes) -> None:
        raise ValueError(self.text)


def test_the_stand_in_for_the_parser_carries_neither_the_text_nor_the_chain_of_a_value_error():
    from glasses.server_runner import _TypeOnlyParser  # here, so that the file still collects without the class

    token = fake_token()
    with pytest.raises(BadHttpMessage) as caught:
        _TypeOnlyParser(QuotingParser(f"netloc {token} is bad")).feed_data(b"x")
    assert token not in str(caught.value) and token not in caught.value.message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__  # the ValueError is not printed
