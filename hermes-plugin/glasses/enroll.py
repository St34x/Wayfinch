# SPDX-License-Identifier: Apache-2.0
"""The ``/enroll`` relay: the plugin's half of device enrolment (brief section 7.2). It checks the HTTP request and
forwards ``{spki, code, sig}`` to the device-CA signer's ``enrol`` op; the window, the one-time code, the proof of
possession, issuance, the token and the audit line are the signer's. No route is registered here: the protocol
server (planned, T1.07) mounts ``/enroll`` for every method (``add_route("*", "/enroll", relay.handle)``) so the peer
check precedes the 405; a POST-only route would answer other methods with the router's 405 before ``handle`` runs.

Order inside ``handle``, each refusal before any signer call: a peer ``peer_allowed`` refuses -> 404 (default
``is_loopback_peer``: the transport's peer address only; client-cert and forwarded headers are never read); not POST
-> 405; ``RateLimiter`` (global, 6 a minute by default) -> 429; ``Content-Type`` not ``application/json`` -> 400;
``Content-Length`` or the body read so far over ``BODY_CAP`` -> 413; not one strict JSON object (UTF-8, no duplicate
key, no NaN/Infinity) holding exactly the string keys ``spki``, ``code``, ``signature`` -> 400; ``MAX_IN_FLIGHT``
relays already running -> 429; after ``close()`` -> 503. Then ``SignerClient.enrol`` runs in a thread of the relay's
own ``MAX_IN_FLIGHT``-worker executor, never the loop's shared default one (its own 340 s deadline is the only one).
A relay holds its slot until that thread ends, even when the request goes away; a request cancelled while its thread
is still queued for the executor frees its slot at once and the call is never made. ``close()`` shuts the executor
down without waiting (the protocol server, planned T1.07, registers it in the app's ``on_cleanup``).
The request and its answers, as the phone sees them: ``protocol/README.md``, section "HTTP ``/enroll``".

Answers: success -> 200 JSON ``{cert, token, deviceId}`` with ``Cache-Control: no-store``; ``enrol_refused`` -> the
same 404 as an unknown path; ``bad_proof``, ``known_key``, ``bad_request`` or a field format the client refuses
before sending -> one generic 400; ``rate_limited`` -> 429; any other signer error or any other exception in the
worker -> 503 and one WARNING naming only the exception type. Bodies are fixed texts: no submitted value, code, token
or exception text reaches a body or a log.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from aiohttp import web

from . import signer_protocol as proto
from .signer_client import EnrolResult, SignerClient

BODY_CAP = 4096  # bytes
MAX_IN_FLIGHT = 2  # relays waiting on the signer at once (each holds a thread up to the 340 s enrol deadline)
WINDOW_S = 60.0
_KEYS = frozenset({"spki", "code", "signature"})
_BAD_REQUEST = frozenset({"bad_proof", "known_key", "bad_request"})
_LOG = logging.getLogger(__name__)

Fields = tuple[str, str, str]
Outcome = EnrolResult | Exception


def is_loopback_peer(request: web.BaseRequest) -> bool:
    """True only when the transport's peer is a loopback IP address (no transport, a unix socket or junk: False)."""
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
        return False
    try:
        return ipaddress.ip_address(peer[0]).is_loopback
    except ValueError:
        return False


class RateLimiter:
    """At most *per_minute* ``allow()`` answers True in any sliding 60 s window (one global budget)."""

    def __init__(self, per_minute: int = 6, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute, self._clock = per_minute, clock
        self._stamps: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= WINDOW_S:
            self._stamps.popleft()
        if len(self._stamps) >= self._per_minute:
            return False
        self._stamps.append(now)
        return True


class EnrollRelay:
    """The ``/enroll`` handler; *client_factory* builds the signer client per request (e.g. ``client_from_config``)."""

    def __init__(
        self,
        client_factory: Callable[[], SignerClient],
        *,
        peer_allowed: Callable[[web.BaseRequest], bool] = is_loopback_peer,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._client_factory, self._peer_allowed = client_factory, peer_allowed
        self._limiter = RateLimiter() if limiter is None else limiter
        self._in_flight = 0
        self._closed = False
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_IN_FLIGHT, thread_name_prefix="glasses-enroll"
        )

    def close(self) -> None:
        """Stops new signer calls (503) and cancels the queued ones; a running call ends on its own."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not self._peer_allowed(request):
            raise web.HTTPNotFound()
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["POST"])
        if not self._limiter.allow():
            raise web.HTTPTooManyRequests()
        fields = _parse(await _read_body(request))
        if self._in_flight >= MAX_IN_FLIGHT:
            raise web.HTTPTooManyRequests()
        if self._closed:
            raise web.HTTPServiceUnavailable()
        return _answer(await self._relay(fields))

    async def _relay(self, fields: Fields) -> Outcome:
        """Holds one slot until the worker thread ends; a request cancelled before its thread starts frees it here."""
        self._in_flight += 1
        claim = threading.Lock()  # taken once: by the worker when it starts, or here if the request is cancelled first
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, self._enrol, loop, fields, claim)
        except asyncio.CancelledError:
            if claim.acquire(blocking=False):
                self._release()
            raise

    def _enrol(self, loop: asyncio.AbstractEventLoop, fields: Fields, claim: threading.Lock) -> Outcome:
        """In a worker thread; the slot is released when the thread ends, even if the awaiting request is gone. Any
        exception becomes the outcome (a signer refusal maps to its answer, anything else to the fixed 503)."""
        if not claim.acquire(blocking=False):
            raise asyncio.CancelledError  # the request was cancelled before this thread started and freed its slot
        spki, code, signature = fields
        try:
            return self._client_factory().enrol(spki, code, sig=signature)
        except Exception as error:  # noqa: BLE001 - mapped by _answer; only its type name is ever logged
            return error.with_traceback(None)  # returned without its traceback
        finally:
            loop.call_soon_threadsafe(self._release)

    def _release(self) -> None:
        self._in_flight -= 1


async def _read_body(request: web.Request) -> bytes:
    """The body, read until EOF but never more than ``BODY_CAP`` + 1 bytes; 400/413 on the checks before it."""
    if request.content_type != "application/json":
        raise web.HTTPBadRequest()
    length = request.content_length
    if length is not None and length > BODY_CAP:
        raise _too_large()
    body = bytearray()
    while len(body) <= BODY_CAP:
        chunk = await request.content.read(BODY_CAP + 1 - len(body))
        if not chunk:
            break
        body += chunk
    if len(body) > BODY_CAP:
        raise _too_large()
    return bytes(body)


def _too_large() -> web.HTTPRequestEntityTooLarge:
    return web.HTTPRequestEntityTooLarge(BODY_CAP, BODY_CAP + 1, text="413: Request Entity Too Large")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")
    return obj


def _refuse_constant(_name: str) -> object:
    raise ValueError("NaN and Infinity are not JSON")


def _parse(body: bytes) -> Fields:
    """``(spki, code, signature)`` of a strict JSON object with exactly those string keys, else 400."""
    try:
        message = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):  # RecursionError: nesting deeper than this interpreter's JSON parser allows
        message = None
    if not isinstance(message, dict) or message.keys() != _KEYS:
        raise web.HTTPBadRequest()
    spki, code, signature = message["spki"], message["code"], message["signature"]
    if type(spki) is not str or type(code) is not str or type(signature) is not str:
        raise web.HTTPBadRequest()
    return spki, code, signature


def _answer(outcome: Outcome) -> web.Response:
    if isinstance(outcome, EnrolResult):
        body = {"cert": outcome.cert, "token": outcome.token, "deviceId": outcome.device_id}
        return web.json_response(body, headers={"Cache-Control": "no-store"})
    code = outcome.code if isinstance(outcome, proto.SignerRefusal) else None
    if code == "enrol_refused":
        raise web.HTTPNotFound()
    if code in _BAD_REQUEST or isinstance(outcome, proto.RequestRefused):
        raise web.HTTPBadRequest()
    if code == "rate_limited":
        raise web.HTTPTooManyRequests()
    _LOG.warning("enrol relay: signer call failed (%s)", type(outcome).__name__)
    raise web.HTTPServiceUnavailable()
