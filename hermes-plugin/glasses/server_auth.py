# SPDX-License-Identifier: Apache-2.0
"""Authentication of the ``/ws`` HTTP upgrade request (T1.07a): everything that happens before a WebSocket exists.

``authenticate(request, deps, fingerprint)`` returns the device or raises an aiohttp ``HTTPException``; the caller
lets it propagate, so a refused request never reaches ``WebSocketResponse.prepare``. Order, each answer fixed JSON:

1. ``deps.gate_ok_async()`` (the allow-all gate, T1.68) false, failing or not answering within ``GATE_TIMEOUT`` (5 s)
   -> ``503`` ``{"error":"unavailable"}``; a refusal is a fixed DEBUG line (the adapter logs the gate on change), a
   failing check one WARNING per exception type, never with a value. Nothing else of the request is read first here;
   the server has already checked the peer, verified the client certificate and taken an upgrade slot
   (``server.py``), so a request without a valid certificate gets ``401`` even while the gate is open.
2. ``Authorization: Bearer <token>``: present exactly once and at most ``HEADER_CAP`` (512) bytes, else ``401``
   ``{"error":"unauthorized"}``. The fingerprint is the caller's argument: the SHA-256 of the client certificate that
   ``edge_cert`` verified (T1.184c); no header names a fingerprint here.
3. A token or fingerprint of the wrong shape (the registry's ``TOKEN_RE`` / ``norm_fp``) -> ``401`` before any thread
   hop or registry read: a malformed credential costs nothing.
4. ``registry.lookup`` (one read of the file, for a success and for every failure) runs in ``RegistryHop``: a small
   pool of this module's own
   (``HOP_WORKERS`` threads, never the loop's default executor, which the gateway shares) behind a bound of
   ``HOP_PENDING_MAX`` requests in flight, a request that hangs up keeping its slot until a worker has finished its
   item; beyond it ``503`` without a registry call (a flood of well-formed wrong upgrades cannot starve other
   callers of the default executor). No registry file I/O on the event loop. Any exception of the registry (or an
   answer of the wrong type) -> ``401`` (fail closed), logged as the exception type only: one WARNING when a failure
   kind appears, DEBUG while it repeats, until the call succeeds again (the same for the
   gate and the revocation check). A wrong token or a mismatched fingerprint -> ``401``. The owner of the
   ``AuthDeps`` calls ``hop.close()`` on shutdown: every waiting request gets ``503`` at once (a queued read is dropped,
   a running one is not joined) and later requests get ``503``; so does a ``run`` that loses the race with ``close()``.
5. Every revoked path -> ``403`` built by ``_revoked`` only, the single place that writes the marker
   ``{"error":"device_revoked"}`` (the phone wipes on it): the registry fingerprint tombstone, a registry
   entry with ``revokedAt``, or the injected ``is_revoked(deviceId)`` (an in-memory check: it must not block): an exact
   ``True`` is revoked, an exact ``False`` admits; a check that raises or answers anything else (``None``, ``0``, a
   coroutine) is ``503`` (still refused, but the phone must not wipe on a wiring bug). ``ReadPending`` (the check's
   data is being re-read, ``revoked_ids``) is no failure: ``revoked_now`` waits for that read and asks once more (the
   upgrade is held, not refused; still pending: ``503``, DEBUG). Each ``403`` first calls
   ``on_revoked`` (``note_revoked``) with the device id, ``None`` for a tombstone (the outbox purge, T1.09b).

Every ``503`` carries ``Retry-After: 5`` (``unavailable()``; the upgrade cap in ``server`` uses the same builder).

No token, token hash, fingerprint or exception text reaches a response, a log line or a raised exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from aiohttp import web

from .registry import TOKEN_RE, Device
from .registry_parse import norm_fp

_LOG = logging.getLogger(__name__)

HEADER_CAP = 512  # bytes, per credential header
GATE_TIMEOUT = 5.0  # seconds; above the adapter's own bound (gate.LOCK_TIMEOUT + 1 = 3 s); gate.py is not imported
RETRY_AFTER_SEC = 5  # the ``Retry-After`` of every ``503`` of ``/ws``
HOP_WORKERS = 4  # registry reads at once, in threads of the hop's own: the loop's default executor stays free
HOP_PENDING_MAX = 32  # requests in the hop (running + queued); the next one is 503 without a registry read
AUTH_HEADER = "Authorization"
_BEARER = "bearer "
_JSON = "application/json"


class RegistryPort(Protocol):
    """The read of ``registry.Registry`` the upgrade needs: one file read, never on the loop."""

    def lookup(self, token: str, fingerprint: str) -> tuple[Device | None, bool]: ...


_T = TypeVar("_T")


class HopFull(Exception):
    """The registry hop has ``pending_max`` requests in it, or is closed."""


class ReadPending(Exception):
    """Raised by an injected ``is_revoked`` whose data is being re-read: *read* ends when that read has finished
    (bounded by the reader itself, never raising); ``revoked_now`` waits for it and asks again."""

    def __init__(self, read: asyncio.Future[Any]) -> None:
        super().__init__("glasses: the revocation data is being re-read")
        self.read = read


class TryAgainLater(Exception):
    """Raised by ``on_hello`` that could not check the device (the outbox's sweep could not read the registry): the
    server closes the session ``1013`` before ``welcome``; nothing is replayed and the phone reconnects."""


class RegistryHop:
    """Runs blocking registry reads in a pool of its own and refuses work beyond a bound.

    A request holds its slot until a worker has finished (or dropped) its work item, not until the awaiting handler
    is done: a handler cancelled by a hang-up leaves its item in the pool queue, so the slot count follows the queue.
    ``HopFull`` (a ``503``) also covers a ``run`` that loses the race with ``close()`` and a queued read it drops.
    """

    def __init__(self, workers: int = HOP_WORKERS, pending_max: int = HOP_PENDING_MAX) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="glasses-auth")
        self._pending_max = pending_max
        self._pending = 0
        self._lock = threading.Lock()  # slots are given back from worker threads
        self._closed = False
        self._waiting: set[asyncio.Future[Any]] = set()  # the reads some request is awaiting, for close()

    async def run(self, call: Callable[..., _T], *args: object) -> _T:
        with self._lock:
            if self._closed or self._pending >= self._pending_max:
                raise HopFull
            self._pending += 1
        try:
            work = self._pool.submit(call, *args)
        except RuntimeError:  # lost the race with close(): a server condition (503), not a bad credential (401)
            self._release()
            raise HopFull from None
        except BaseException:
            self._release()
            raise
        work.add_done_callback(lambda _done: self._release())  # on completion or cancel, in whatever thread
        awaited = asyncio.wrap_future(work)
        awaited.add_done_callback(_consume)
        awaited.add_done_callback(self._forget)
        with self._lock:
            self._waiting.add(awaited)
        try:
            return await asyncio.shield(awaited)  # a hang-up must not cancel the item: it keeps its slot
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if awaited.cancelled() and task is not None and task.cancelling() == 0:
                raise HopFull from None  # not this request's cancellation: close() ended the wait for its read
            raise

    def close(self) -> None:
        """Refuse new work and end every wait (a queued read is dropped, a running one is not joined: its request gets
        ``HopFull`` at once and the worker's slot comes back when the read ends); idle threads end; never blocks."""
        with self._lock:
            self._closed = True
            waiting = list(self._waiting)
        self._pool.shutdown(wait=False, cancel_futures=True)
        for future in waiting:
            with contextlib.suppress(RuntimeError):  # the loop is already closed: nobody is waiting
                future.get_loop().call_soon_threadsafe(future.cancel)

    def _forget(self, done: asyncio.Future[Any]) -> None:
        with self._lock:
            self._waiting.discard(done)

    def _release(self) -> None:
        with self._lock:
            self._pending -= 1


def _consume(done: asyncio.Future[Any]) -> None:
    """Mark the outcome of a read nobody awaits any more as retrieved (no 'never retrieved' log line)."""
    if not done.cancelled():
        done.exception()


class _Failures:
    """Which failure of each site is being repeated: a new kind is a WARNING, the same kind again a DEBUG line."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def note(self, site: str, kind: str, message: str) -> None:
        level = logging.DEBUG if self._last.get(site) == kind else logging.WARNING
        self._last[site] = kind
        _LOG.log(level, message, kind)

    def clear(self, site: str) -> None:
        self._last.pop(site, None)


@dataclass(frozen=True)
class AuthDeps:
    gate_ok_async: Callable[[], Awaitable[bool]]
    registry: RegistryPort
    is_revoked: Callable[[str], bool]
    on_revoked: Callable[[str | None], None] | None = None
    hop: RegistryHop = field(default_factory=RegistryHop)
    failures: _Failures = field(default_factory=_Failures, repr=False, compare=False)


def _refusal(kind: type[web.HTTPException], error: str) -> web.HTTPException:
    refusal = kind(text=f'{{"error":"{error}"}}', content_type=_JSON)
    refusal.charset = None  # Content-Type is exactly application/json
    return refusal


def unauthorized() -> web.HTTPException:
    """The fixed ``401`` of ``/ws``: a missing or bad token, a certificate ``edge_cert`` refused, an unknown device."""
    return _refusal(web.HTTPUnauthorized, "unauthorized")


def unavailable() -> web.HTTPException:
    """The ``503`` of every server-side refusal of ``/ws`` (upgrade cap, gate, registry hop, revocation check): it
    carries ``Retry-After: 5`` (``RETRY_AFTER_SEC``, the gate bound), so a client backs off instead of hammering."""
    refusal = _refusal(web.HTTPServiceUnavailable, "unavailable")
    refusal.headers["Retry-After"] = str(RETRY_AFTER_SEC)
    return refusal


def _revoked() -> web.HTTPException:
    return _refusal(web.HTTPForbidden, "device_revoked")


def _header(request: web.Request, name: str) -> str | None:
    """The one value of header *name*, or None when it is absent, repeated or over the byte cap."""
    values = request.headers.getall(name, [])
    if len(values) != 1 or len(values[0].encode("utf-8", "surrogateescape")) > HEADER_CAP:
        return None
    return values[0]


def _credentials(request: web.Request, fingerprint: str) -> tuple[str, str] | None:
    auth = _header(request, AUTH_HEADER)
    if auth is None or auth[: len(_BEARER)].lower() != _BEARER:
        return None
    token = auth[len(_BEARER) :]
    if TOKEN_RE.fullmatch(token) is None or norm_fp(fingerprint) is None:
        return None
    return token, fingerprint


async def _gate_open(deps: AuthDeps) -> bool:
    try:
        answer = await asyncio.wait_for(deps.gate_ok_async(), GATE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - fail closed on any gate failure, log the type only
        deps.failures.note("gate", type(exc).__name__, "glasses ws: allow-all gate check failed (%s)")
        return False
    deps.failures.clear("gate")
    return answer is True


def _lookup(registry: RegistryPort, token: str, fingerprint: str) -> tuple[Device | None, bool]:
    """``(device, False)`` for an active device, ``(None, True)`` for a revoked fingerprint, else ``(None, False)``.

    An answer of the wrong type is a registry failure (``TypeError``): only an exact ``Device`` and an exact ``True`` /
    ``False`` count (identity and ``type``, not ``isinstance``: a ``Mock(spec=...)`` claims any class).
    """
    device, tombstone = registry.lookup(token, fingerprint)
    if (device is not None and type(device) is not Device) or (tombstone is not True and tombstone is not False):
        raise TypeError
    return device, tombstone


async def _find(deps: AuthDeps, token: str, fingerprint: str) -> tuple[Device | None, bool]:
    """``_lookup`` in the registry hop; a registry failure is ``(None, False)`` (no device, so 401)."""
    try:
        found = await deps.hop.run(_lookup, deps.registry, token, fingerprint)
    except HopFull:
        _LOG.debug("glasses ws: registry hop full; refusing")
        raise unavailable() from None
    except Exception as exc:  # noqa: BLE001 - fail closed on any registry failure, log the type only
        deps.failures.note("registry", type(exc).__name__, "glasses ws: registry error (%s); refusing")
        return None, False
    deps.failures.clear("registry")
    return found


def check_revoked(deps: AuthDeps, device_id: str) -> bool:
    """The injected check's answer: ``True`` revoked, ``False`` active; ``ReadPending`` passes through (no log line: a
    routine re-read); any other raise or answer is ``503``."""
    try:
        answer = deps.is_revoked(device_id)
    except ReadPending:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on any failure of the injected check, log the type only
        deps.failures.note("revocation", type(exc).__name__, "glasses ws: revocation check failed (%s); refusing")
        raise unavailable() from None
    if answer is True or answer is False:  # identity, not isinstance: an object whose __class__ claims bool is not one
        deps.failures.clear("revocation")
        return answer
    if inspect.iscoroutine(answer):
        answer.close()  # an ``async def`` check was wired: never awaited, so close it quietly
    kind = type(answer).__name__
    deps.failures.note("revocation", kind, "glasses ws: revocation check answered a %s, not a boolean; refusing")
    raise unavailable()


async def revoked_now(deps: AuthDeps, device_id: str) -> bool:
    """``check_revoked``; a ``ReadPending`` check is asked again once its read has finished (the frame or upgrade is
    held for the read, which bounds itself, not dropped). Still pending then: ``503``, a DEBUG line (fail closed)."""
    try:
        return check_revoked(deps, device_id)
    except ReadPending as pending:
        await asyncio.wait((pending.read,))  # never raises; a cancelled caller leaves the read running
    try:
        return check_revoked(deps, device_id)
    except ReadPending:
        _LOG.debug("glasses ws: revocation re-read still pending; refusing")
        raise unavailable() from None


def note_revoked(deps: AuthDeps, device_id: str | None) -> None:
    """Tell ``on_revoked`` (on the loop, must not block) that a revoked device was refused; a raising hook is logged by
    type and the refusal stands."""
    try:
        if deps.on_revoked is not None:
            deps.on_revoked(device_id)
    except Exception as exc:  # noqa: BLE001 - the device is refused either way; type only
        _LOG.warning("glasses ws: revoked-device hook failed (%s)", type(exc).__name__)


async def authenticate(request: web.Request, deps: AuthDeps, fingerprint: str) -> Device:
    """The active device behind this upgrade request and its verified certificate *fingerprint*; raises
    ``HTTPException`` (503, 401 or 403) otherwise."""
    if not await _gate_open(deps):
        _LOG.debug("glasses ws: upgrade refused: allow-all gate")
        raise unavailable()
    credentials = _credentials(request, fingerprint)
    if credentials is None:
        raise unauthorized()
    device, tombstone = await _find(deps, *credentials)
    if tombstone is True:
        note_revoked(deps, None)
        raise _revoked()
    if device is None:
        raise unauthorized()
    if device.revoked_at is not None or await revoked_now(deps, device.device_id):
        note_revoked(deps, device.device_id)
        raise _revoked()
    return device
