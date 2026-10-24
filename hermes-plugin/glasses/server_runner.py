# SPDX-License-Identifier: Apache-2.0
"""The aiohttp runner of the WS server (T1.07b): ``build_runner(app)`` is the ONE way to run the app (it starts no
listener: ``serve`` binds it to the edge listener). It differs from ``web.AppRunner`` in three ways: handlers
are not cancelled on a hang-up (``handler_cancellation`` off, the mode the bound of ``server_auth.RegistryHop`` and
the ``/enroll`` relay are written for; ``TestServer`` forces it on: knowledge-base entry
``aiohttp-testserver-forces-handler-cancellation``), a first request that
the HTTP parser refuses (an ``HttpProcessingError``, or any ``ValueError`` raised by the parser's ``feed_data``, yarl's
for an absolute-form target among them, which the parser stand-in turns into a ``BadHttpMessage``) is answered with the
status phrase and logged by type only (aiohttp quotes the offending line, which may be a ``Bearer`` line). A
``ValueError`` raised elsewhere, e.g. by the payload sink, is not converted. The tail a client pipelines behind a
refused upgrade, which aiohttp parses again outside ``handle_error`` (T1.239), is logged the same way but gets no
answer: its ``401`` is lost by design and the connection ends. Not every bad request is answered: a request target the
parser accepts but the request factory refuses (a bad port) can leave the connection open and unanswered, a known gap
tracked as a follow-up. The access log has no query string.
"""

from __future__ import annotations

import inspect
import logging
import sys
from http import HTTPStatus
from typing import Any, cast

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from aiohttp.http_exceptions import BadHttpMessage, HttpProcessingError

from .edge_listener import EdgeListener

TESTED_AIOHTTP = "3.14"  # major.minor the swap below was tested on; the aiohttp row of docs/dependencies.md pins it


class AiohttpDrift(RuntimeError):
    """The installed aiohttp is not the one the class swap was written for."""


class _TypeOnlyParser:
    """The request parser as ``_Handler`` uses it. Besides ``HttpProcessingError`` the parser raises a plain
    ``ValueError`` (yarl, for an absolute-form target; with the pure-Python parser its text quotes the netloc). aiohttp
    logs such an error, traceback included, from ``asyncio`` (a first request) or ``start()`` (the tail behind a refused
    upgrade), so every ``ValueError`` of ``feed_data`` becomes a ``BadHttpMessage`` with a fixed text here (one raised
    elsewhere, e.g. by the payload sink, is not converted). A first request is then answered 400; the re-parsed tail
    behind a refused upgrade gets no answer (the connection ends). Every log path that already filters
    ``HttpProcessingError`` logs its type only."""

    def __init__(self, parser: Any) -> None:
        self._parser = parser

    def feed_data(self, data: bytes) -> Any:
        try:
            return self._parser.feed_data(data)
        except ValueError:
            raise BadHttpMessage("Bad Request") from None  # not chained: the ValueError may quote the bytes

    def __getattr__(self, name: str) -> Any:  # set_upgraded, message_consumed, pause_reading ...
        return getattr(self._parser, name)


class _Handler(web.RequestHandler):
    """aiohttp quotes the offending line of a request its parser refuses in the 400 body and in an ERROR record; a
    ``Bearer`` line is a credential, so such a request is answered with the status phrase and logged by type only. The
    parser also runs outside ``handle_error``: the tail pipelined behind an upgrade is parsed again in
    ``finish_response``, and its error reaches ``log_exception``, which therefore filters the same way."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        if not callable(getattr(self._parser, "feed_data", None)):
            raise AiohttpDrift(f"aiohttp {aiohttp.__version__}: the request parser has no feed_data")
        self._parser = cast("Any", _TypeOnlyParser(self._parser))  # it stands in for the parser BaseProtocol types

    def handle_error(
        self, request: web.BaseRequest, status: int = 500, exc: BaseException | None = None, message: str | None = None
    ) -> web.StreamResponse:
        if not isinstance(exc, HttpProcessingError):
            return super().handle_error(request, status, exc, message)
        self._log_parser_error(exc)
        response = web.Response(status=status, text=HTTPStatus(status).phrase)
        response.force_close()
        return response

    def log_exception(self, *args: Any, **kw: Any) -> None:
        exc = kw.get("exc_info")  # aiohttp passes the exception itself; `True` means the one being handled
        if exc is True:
            exc = sys.exc_info()[1]
        if isinstance(exc, HttpProcessingError):
            self._log_parser_error(exc)
        else:
            super().log_exception(*args, **kw)

    def _log_parser_error(self, exc: HttpProcessingError) -> None:
        self.logger.debug("glasses ws: request refused by the HTTP parser (%s)", type(exc).__name__)


class _Server(web.Server):
    def __call__(self) -> web.RequestHandler:
        return _Handler(self, loop=self._loop, **self._kwargs)


class _Runner(web.AppRunner):
    async def _make_server(self) -> web.Server:
        server = await super()._make_server()
        missing = [name for name in ("_loop", "_kwargs") if not hasattr(server, name)]  # instance attributes
        if missing:
            raise AiohttpDrift(f"aiohttp {aiohttp.__version__}: web.Server has no {', '.join(missing)}")
        server.__class__ = _Server  # the same server, whose connections are _Handler instances
        return server

    async def setup(self) -> None:
        """``AppRunner.setup``, then proof that the swap happened; a drift undoes the setup (the app's cleanup)."""
        try:
            await super().setup()
            if self.server is None or self.server.__class__ is not _Server:
                raise AiohttpDrift(f"aiohttp {aiohttp.__version__}: the server class swap was not applied")
            self.server()  # one handler, never connected: its construction checks the parser the handler wraps
        except AiohttpDrift:
            await self.cleanup()
            raise


class _AccessLog(AbstractAccessLogger):
    """One INFO line per request without the query string (a credential a misconfigured client puts in the URL stays
    out of the log): ``remote "METHOD path" status ms`` (integer milliseconds, so a slow 503 of a stalled gate or
    registry shows)."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        self.logger.info(
            '%s "%s %s" %s %d',
            request.remote,
            request.method,
            request.rel_url.raw_path,
            response.status,
            int(time * 1000),
        )

    @property
    def enabled(self) -> bool:
        return self.logger.isEnabledFor(logging.INFO)


def check_aiohttp() -> None:
    """Raise ``AiohttpDrift`` unless the installed aiohttp has what the class swap relies on (what can be seen on the
    classes; ``_Runner`` checks the instance attributes and the swap itself in ``setup``)."""
    found = aiohttp.__version__
    if ".".join(found.split(".")[:2]) != TESTED_AIOHTTP:
        raise AiohttpDrift(f"aiohttp {found} found, {TESTED_AIOHTTP}.x tested")
    if "_make_server" not in vars(web.AppRunner):
        raise AiohttpDrift(f"aiohttp {found}: AppRunner._make_server is missing")
    for name in ("handle_error", "log_exception"):  # the two hooks _Handler overrides to keep parser errors out of logs
        theirs = getattr(web.RequestHandler, name, None)
        if not callable(theirs):
            raise AiohttpDrift(f"aiohttp {found}: RequestHandler.{name} is missing")
        if list(inspect.signature(theirs).parameters) != list(inspect.signature(getattr(_Handler, name)).parameters):
            raise AiohttpDrift(f"aiohttp {found}: RequestHandler.{name} has other parameters")


def build_runner(app: web.Application) -> web.AppRunner:
    """The one way to run the app (it starts no listener): handlers are not cancelled on a hang-up, parse errors quote
    nothing and the access log has no query string. Raises ``AiohttpDrift`` (here or in ``setup``) when aiohttp is not
    the tested one."""
    check_aiohttp()
    return _Runner(app, handler_cancellation=False, access_log_class=_AccessLog)


async def serve(app: web.Application, listener: EdgeListener) -> web.AppRunner:
    """Run *app* with the one runner (``build_runner``) on the edge unix-socket *listener*, the only site; the caller
    stops the listener and then cleans the runner up. ``AiohttpDrift`` or ``EdgeSocketError`` leave nothing running."""
    runner = build_runner(app)
    await runner.setup()
    server = runner.server
    if server is None:  # setup() always sets it; a drift there raised already
        raise AiohttpDrift("aiohttp: the runner has no server after setup")
    try:
        await listener.start(server)
    except BaseException:
        await runner.cleanup()
        raise
    return runner
