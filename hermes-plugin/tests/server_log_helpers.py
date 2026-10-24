# SPDX-License-Identifier: Apache-2.0
"""Shared by the tests that send a request a parser refuses to a real loopback server built through ``build_runner``
(T1.239): every logger is captured at DEBUG and every record is rendered with a formatter, so ``exc_text`` counts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from server_auth_helpers import creds, upgrade_headers
from server_helpers import app_for, serving, wiring

from glasses.server import _CAP

FRESH_REQUEST = b"GET /nothing HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"


class Capture(logging.Handler):
    """Every record of every logger, rendered the way a log file would show it (message and traceback)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.rendered: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.rendered.append(logging.Formatter().format(record))

    def of(self, logger: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.name == logger]

    def errors(self, logger: str) -> list[str]:
        return [r.getMessage() for r in self.of(logger) if r.levelno >= logging.ERROR]


@contextlib.contextmanager
def capturing() -> Iterator[Capture]:
    root, capture = logging.getLogger(), Capture()
    before = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(capture)
    try:
        yield capture
    finally:
        root.removeHandler(capture)
        root.setLevel(before)


def fake_token() -> str:
    return "FAKE-" + os.urandom(12).hex()  # built at run time; the tree holds no token-looking literal


def upgrade_request() -> bytes:  # always a wrong token: the upgrade is refused
    headers = {**upgrade_headers(), **creds("x" * 43)}
    return ("GET /ws HTTP/1.1\r\nHost: x\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode()


@dataclass
class Outcome:
    reply: bytes
    slots: dict[str, int]
    fresh: bytes
    log: Capture


async def end_of_connection(port: int, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    try:
        reply = await asyncio.wait_for(reader.read(), 10)  # to the end of the connection: no hang
    except ConnectionResetError:
        reply = b""
    writer.close()
    return reply


def exchange(tmp_path: Path, data: bytes, **changes: Any) -> Outcome:
    """Send *data* to a server whose deps have *changes*: the reply to its end, the cap slots, then a fresh request."""

    async def main(capture: Capture) -> Outcome:
        app = app_for(wiring(tmp_path), **changes)
        app.router.add_get("/boom", boom)
        async with serving(app) as port:
            reply = await end_of_connection(port, data)
            fresh = await end_of_connection(port, FRESH_REQUEST)
            return Outcome(reply, dict(app[_CAP]._open), fresh, capture)

    with capturing() as capture:
        return asyncio.run(asyncio.wait_for(main(capture), 30))


async def boom(_request: web.Request) -> web.Response:
    raise RuntimeError("handler failed")


def assert_quiet_and_alive(out: Outcome, token: str) -> None:
    """SAT-L1-6 and the log rules: nothing quotes the token or carries a traceback, the server is intact."""
    assert not [text for text in out.log.rendered if token in text], "a log record quotes the request bytes"
    assert not [text for text in out.log.rendered if "Traceback" in text], "a log record carries a traceback"
    assert [r.getMessage() for r in out.log.records if r.levelno >= logging.ERROR] == []  # no logger, asyncio included
    assert out.slots == {}  # the upgrade slot of the certificate is free again
    assert out.fresh.startswith(b"HTTP/1.1 404"), out.fresh[:80]  # and it answers a fresh connection
