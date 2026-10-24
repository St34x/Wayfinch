# SPDX-License-Identifier: Apache-2.0
"""WebSocket server (T1.07b, T1.07c): an aiohttp ``Application`` factory with no listener of its own.

``build_app(deps)`` registers ``/ws`` and ``/enroll``; ``build_runner(app)`` is the ONE way to run it (no listener:
``serve`` binds it to the edge listener). ``app[CONNECTIONS]`` is the connection table (``close_device``: the
revocation push, T1.63a).

The ``/ws`` handler, in this order:

1. ``deps.peer_allowed(request)``: a peer it does not allow (or a raising predicate: one WARNING per new kind of
   failure, DEBUG while it repeats, the type name only) gets the unknown-path ``404`` for every method (also
   ``POST /ws``), with no gate, registry or ``is_revoked`` call. An allowed peer gets ``405`` for any method but
   ``GET``.
2. ``deps.client_fingerprint(request)`` (``edge_cert``: the forwarded client certificate verified against the device
   CA bundle; no registry read): no verified certificate (or a raising checker, logged by type) is the fixed ``401``
   before the cap, the gate or any registry read.
3. The cap on upgrades in flight per verified certificate fingerprint (``connections.UpgradeCap``, 8): over it ``503``
   before the gate, the registry or any thread hop, another certificate unaffected; the slot is held until the
   WebSocket exists. The registry hop's bound of 32 requests stays the global floor. How often one certificate reaches
   the plugin is the edge's: we recommend a ``limit_req`` zone keyed on
   ``$ssl_client_fingerprint`` (e.g. 30 r/m, burst 10 for ``/ws``, status 429).
4. ``server_auth.authenticate`` with that fingerprint: 503 / 401 / 403 before any WebSocket exists. From the cap to the
   WebSocket the socket is not read (only aiohttp's first read or reads are parsed at ``prepare``); every refusal,
   hang-up or cancellation reads again unless the transport is closing. A refused upgrade with a malformed request
   pipelined behind it loses its answer by design: aiohttp parses that tail again before it prepares the response
   (``Connection: close`` does not help) and ends the connection; no legitimate client pipelines behind an upgrade.
   The parser error (an ``HttpProcessingError``, or any ``ValueError`` of the parser's ``feed_data``, yarl's among
   them) is logged by type only (``server_runner``). A client that hangs up before the upgrade completes ends the
   request quietly (DEBUG line, access-log status 499; no error record or 500).
5. The connection: a newer one of the same device replaces the older (closed ``4409``); ``deps.on_authenticated`` is
   called with the device; every frame the server sends goes through the connection's one send queue and writer.
6. The session (``server_session``, T1.249): ``hello`` / ``welcome``, ``ping`` / ``pong``, the per-frame revocation
   check, ``deps.on_frame``, the read pause and the inbound byte budget.

``/enroll`` is routed for every method to the ONE ``EnrollRelay`` of the app (its peer check precedes its 405); it never
upgrades. On shutdown (``on_shutdown``, before aiohttp waits for the handlers; ``on_cleanup`` repeats it, closes the
relay, on the loop thread) the registry hop is closed first: an upgrade waiting on a registry read is answered ``503``
at once (the worker is not joined); then every connection is closed ``1001``, also a device whose upgrade completes
afterwards (``ConnectionTable.admit``).

``welcome`` carries no ``seq`` (never stored or replayed). With ``deps.on_hello`` (the outbox, T1.09b) ``resumeFrom`` is
what it returns and its replayed frames follow the ``welcome`` in the same send queue (without it ``lastAckedSeq`` + 1,
at most 2**53 - 1). A frame the send queue refuses ends the connection ``1008`` (``Connection.refused``). Planned: a
server-initiated ``ping`` every 25 s, ``4400`` for an unsupported protocol.

``serve(app, listener)`` runs the app (``build_runner``, ``server_runner.py``, re-exported) on the edge unix socket
(``edge_listener``), the only site. ``build_runner`` logs one access line per request, ``remote "METHOD path" status
ms`` (no query string, integer ms), keeps ``handler_cancellation`` off (a hang-up does not cancel a running handler,
the mode ``server_auth.RegistryHop`` and the relay are written for; ``TestServer`` forces it on: knowledge-base entry
``aiohttp-testserver-forces-handler-cancellation``) and checks the aiohttp it runs on (it swaps an aiohttp class): a
drift (another major.minor than ``TESTED_AIOHTTP``, a missing internal, a swap not applied) is one ``AiohttpDrift``
(a ``RuntimeError``) from ``build_runner`` or ``setup``, at start.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import WSCloseCode, web

from .connections import Connection, ConnectionTable, TooManyUpgrades, UpgradeCap
from .enroll import EnrollRelay
from .envelope import FrameSchema
from .generated.protocol_types import Frame
from .registry import Device
from .server_auth import AuthDeps, RegistryPort, authenticate, unauthorized, unavailable
from .server_auth import TryAgainLater as TryAgainLater  # what an ``on_hello`` raises when it cannot check the device
from .server_runner import AiohttpDrift as AiohttpDrift
from .server_runner import build_runner as build_runner
from .server_runner import serve as serve
from .server_session import CLOSE_REVOKED as CLOSE_REVOKED
from .server_session import MAX_MSG_SIZE as MAX_MSG_SIZE
from .server_session import _frames
from .server_session import _now as _now  # every frame on the edge takes its id and time from here (adapter)
from .server_session import _ulid as _ulid
from .signer_client import NotConfigured, SignerClient

_LOG = logging.getLogger(__name__)

_WS_LIMIT = MAX_MSG_SIZE + 1  # aiohttp closes a message of >= max_msg_size bytes (reader_py.py): one more
MAX_AUDIO_SEC = 30  # the schema forbids promising a longer clip
_CLIENT_LEFT = 499  # access-log status of a request whose client hung up before the upgrade (nothing is sent)


@dataclass(frozen=True)
class ServerDeps:
    """Everything injected into ``build_app`` (``build_app`` builds the one ``AuthDeps`` from the first three).

    ``client_fingerprint(request)`` is the verified certificate fingerprint of an upgrade or ``None``
    (``edge_cert.EdgeCert.fingerprint``). ``on_authenticated(device)`` is called on the loop after each successful
    upgrade and must not block (intended hook: ``registry_signer_file.LastSeen.touch``, T1.179); a failure is logged
    by type and does not end the session. ``signer_client`` builds the signer client of the ``/enroll`` relay; without
    it ``/enroll`` answers 503 after its own checks. ``on_hello(deviceId, lastAckedSeq, conn)`` gives ``(resumeFrom,
    frames to replay)``; ``on_closed(deviceId, conn)`` is called on the loop when that connection's frame loop ends,
    on every exit (``Outbox.closed``); ``on_revoked`` is ``server_auth.note_revoked``'s hook (also for a session closed
    ``4403``)."""

    gate_ok_async: Callable[[], Awaitable[bool]]
    registry: RegistryPort
    is_revoked: Callable[[str], bool]
    peer_allowed: Callable[[web.BaseRequest], bool]
    client_fingerprint: Callable[[web.BaseRequest], str | None]
    on_frame: Callable[[Device, Frame], Awaitable[None]]
    schema: FrameSchema
    max_audio_sec: int
    on_authenticated: Callable[[Device], None] | None = None
    signer_client: Callable[[], SignerClient] | None = None
    on_hello: Callable[[str, int, Connection], Awaitable[tuple[int, list[str]]]] | None = None
    on_revoked: Callable[[str | None], None] | None = None
    on_closed: Callable[[str, Connection], None] | None = None


CONNECTIONS = web.AppKey("connections", ConnectionTable)
_AUTH = web.AppKey("auth", AuthDeps)
_DEPS = web.AppKey("deps", ServerDeps)
_CAP = web.AppKey("upgrade_cap", UpgradeCap)
_RELAY = web.AppKey("enroll_relay", EnrollRelay)


def _peer_ok(deps: ServerDeps, auth: AuthDeps, request: web.BaseRequest) -> bool:
    try:
        allowed = deps.peer_allowed(request) is True
    except Exception as exc:  # noqa: BLE001 - fail closed on a failing predicate, log the type only
        auth.failures.note("peer", type(exc).__name__, "glasses ws: peer check failed (%s); refusing")  # WARNING once
        return False
    auth.failures.clear("peer")
    return allowed


def _verified_fingerprint(deps: ServerDeps, auth: AuthDeps, request: web.BaseRequest) -> str | None:
    try:
        fingerprint = deps.client_fingerprint(request)
    except Exception as exc:  # noqa: BLE001 - fail closed on a failing checker, log the type only
        auth.failures.note("cert", type(exc).__name__, "glasses ws: certificate check failed (%s); refusing")
        return None
    auth.failures.clear("cert")
    return fingerprint if type(fingerprint) is str else None


def _announce(deps: ServerDeps, device: Device) -> None:
    if deps.on_authenticated is None:
        return
    try:
        deps.on_authenticated(device)
    except Exception as exc:  # noqa: BLE001 - a failing hook must not end the session; type only
        _LOG.warning("glasses ws: on_authenticated failed (%s)", type(exc).__name__)


async def _serve(request: web.Request, ws: web.WebSocketResponse, device: Device) -> web.WebSocketResponse:
    """The connection of an authenticated device: registered (replacing an older one), served, then forgotten."""
    app = request.app
    deps, table = app[_DEPS], app[CONNECTIONS]
    conn = Connection(device.device_id, ws, request.transport)
    try:
        with conn.reads_paused():  # unread while admit waits for the replaced connection's close (up to 0.5 s)
            admitted = await table.admit(conn)
        if admitted:  # the pause ended without a turn of the loop in between: the frame loop's first receive() is next
            _announce(deps, device)
            await _frames(conn, device, deps, app[_AUTH])
    except (ConnectionError, OSError) as exc:  # a transport error ends the session, type only
        _LOG.debug("glasses ws: session ended (%s)", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - the dispatch seam or the codec failed: end this session, type only
        _LOG.warning("glasses ws: session failed (%s)", type(exc).__name__)
        await conn.close(WSCloseCode.INTERNAL_ERROR)
    finally:
        table.remove(conn)
        await conn.stop()
    return ws


async def _upgrade(request: web.Request, fingerprint: str) -> tuple[Device, web.WebSocketResponse]:
    """Authenticate the request and complete the WebSocket handshake; any other exit raises. The socket is not read
    from here on (after aiohttp's first read or reads, which ``prepare`` parses): until ``prepare`` installs the
    WebSocket parser aiohttp keeps every byte a client pipelines behind its upgrade request in a tail without a limit,
    and ``prepare`` turns the whole tail into queued frames in one call (T1.184b: loop gap under 250 ms, growth under
    100 MiB). A refusal reads again (the 401/403/503 is answered and the connection may be reused); on
    success the pause is kept for ``_serve``, whose ``reads_paused`` block ends it."""
    app, transport = request.app, request.transport
    if transport is not None:
        transport.pause_reading()
    try:
        with app[_CAP].slot(fingerprint):  # held until the WebSocket exists, not for its lifetime
            device = await authenticate(request, app[_AUTH], fingerprint)
            ws = web.WebSocketResponse(max_msg_size=_WS_LIMIT, autoping=False)
            await ws.prepare(request)
    except BaseException:  # a refusal, a hang-up, a cancellation: never leave the connection unread
        if transport is not None and not transport.is_closing():
            transport.resume_reading()
        raise
    return device, ws


async def _handle(request: web.Request) -> web.StreamResponse:
    app = request.app
    if not _peer_ok(app[_DEPS], app[_AUTH], request):
        raise web.HTTPNotFound
    if request.method != "GET":  # after the peer check: a refused peer cannot tell that /ws exists, whatever the method
        raise web.HTTPMethodNotAllowed(request.method, ["GET"])
    fingerprint = _verified_fingerprint(app[_DEPS], app[_AUTH], request)
    if fingerprint is None:  # before the cap: a refused certificate never holds a slot
        raise unauthorized()
    try:
        device, ws = await _upgrade(request, fingerprint)
    except TooManyUpgrades:
        _LOG.debug("glasses ws: certificate at the upgrade cap; refusing")
        raise unavailable() from None
    except (ConnectionError, OSError) as exc:  # the client hung up before the upgrade completed: nobody to answer
        _LOG.debug("glasses ws: client left during the upgrade (%s)", type(exc).__name__)
        return web.Response(status=_CLIENT_LEFT)
    return await _serve(request, ws, device)


async def _shutdown(app: web.Application) -> None:
    """Before aiohttp waits (60 s) for the handlers of open sockets: upgrades waiting on a registry read get ``503`` at
    once (the worker thread is not joined), then every connection is closed with ``1001``."""
    app[_AUTH].hop.close()
    await app[CONNECTIONS].close_all()


async def _cleanup(app: web.Application) -> None:
    """On the loop thread: end the connections and the hop (no-ops after ``on_shutdown``), then the relay."""
    await _shutdown(app)
    app[_RELAY].close()


def _no_signer() -> SignerClient:
    raise NotConfigured("signer")  # the relay answers 503 and logs the type only


def build_app(deps: ServerDeps) -> web.Application:
    """The ``/ws`` and ``/enroll`` application: exactly one ``AuthDeps`` (registry-hop pool), one connection table and
    one ``EnrollRelay`` per app, closed in ``on_cleanup``."""
    if not 1 <= deps.max_audio_sec <= MAX_AUDIO_SEC:
        raise ValueError(f"max_audio_sec must be 1-{MAX_AUDIO_SEC}")
    app = web.Application()
    app[_DEPS] = deps
    app[CONNECTIONS] = ConnectionTable()
    app[_CAP] = UpgradeCap()
    app[_AUTH] = auth = AuthDeps(deps.gate_ok_async, deps.registry, deps.is_revoked, deps.on_revoked)
    app[_RELAY] = relay = EnrollRelay(deps.signer_client or _no_signer, peer_allowed=lambda r: _peer_ok(deps, auth, r))
    app.router.add_route("*", "/ws", _handle)  # every method: the peer check precedes the 405 (as for /enroll)
    app.router.add_route("*", "/enroll", relay.handle)  # every method: the relay's peer check precedes its 405
    app.on_shutdown.append(_shutdown)  # before aiohttp waits (60 s) for the handlers of open sockets
    app.on_cleanup.append(_cleanup)
    return app
