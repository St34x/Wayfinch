# SPDX-License-Identifier: Apache-2.0
"""``glasses`` platform adapter — skeleton (T1.04).

Runs inside the Hermes gateway and imports Hermes; unit tests import it only against stand-in Hermes modules
(``hermes-plugin/tests/test_adapter.py``). The Hermes objects it reads are typed by small protocols below.
``connect()`` (T1.184b) checks the gate, then serves ``/ws`` and ``/enroll`` on the unix socket
``edge.socket`` to processes of ``edge.user`` only (``edge_listener``, ``edge_auth``; never on TCP), over the signer's
read-only ``devices.json`` (``signer_file_registry``) with ``revoked_ids.RevokedIds`` as ``is_revoked`` (overlay,
outbox, the signer's revoked ids; T1.248), the device identity from the forwarded client certificate verified against
the signer's ``device-ca.pem`` (``edge_cert``) and ``LastSeen.touch`` as ``on_authenticated``; ``disconnect()`` ends
the frame check's stat poll, stops the listener, unlinks the socket it created, closes the app, closes the outbox and
flushes last-seen off the loop.

Inbound (T1.08, ``adapter_router._FrameRouter``): a ``msg.in`` goes through ``glasses.inbound`` (lane checks, dedupe)
and becomes the gateway's ``MessageEvent(TEXT)`` from ``build_source(chat_id="glasses:<lane>", chat_type="dm",
user_id=<deviceId>)``, handed to ``hand_on`` (gate check, then the base class's ``handle_message`` spawns the agent
turn: ``on_frame`` does not wait for it); a refusal is an ``error`` frame on the device's connection; an ``ack`` goes
to the outbox. Every other frame type is logged by type only. A start that fails (gate open, ``edge.socket`` /
``edge.user`` unset or refused, a path that is not a stale socket of this user, an unwritable directory, an aiohttp
drift, an unusable signer setting) is a fatal error with ``connect()`` returning False and nothing left bound. The
error is marked *retryable* on purpose: at the pinned Hermes a non-retryable failure of the only platform makes the
whole gateway exit (``run_startup.py`` exit 78), which would also stop cron and every other profile. Retryable keeps
the gateway up; the reconnect watcher retries with backoff (30 s doubling, capped at 300 s).

Allow-all gate: ``connect()`` (every attempt) and ``handle_message()`` (every inbound event) await ``gate_ok_async()``,
which checks the profiles this process serves through ``glasses.gate.VerdictCache`` (a verdict is reused for one
second, then always recomputed) in the gate's own two worker threads (never the loop's default executor). A caller
that waits longer than two seconds for a blocked recompute is refused, never admitted, and so is the check that runs
it (after about three seconds) and one whose check cannot be submitted. An allow-all switch or a ``*`` allowlist added
to a served profile's ``.env`` after start-up refuses connections and drops events. A refusal is logged at WARNING when
it differs from the one logged last, a return to clean once at INFO. ``gate_ok()`` is the form for code off the loop.

Outbound (T1.09b): ``send()`` stores one ``msg.out`` per recipient (``glasses.outbound``: every non-revoked registry
device holding the lane) in the outbox (``glasses.outbox``) and writes it at once to a welcomed connection; a
``hello`` replays what is queued. ``connect()`` opens and checks ``outbox.sqlite`` (unusable: a failed start) and
purges every revoked device; so do ``send()`` and every ``403`` the server answers a revoked device (``on_revoked``,
with T1.08's dedupe ids). A store or registry failure in ``send()`` is a retryable failed ``SendResult``.

Message length: the long-answer router planned for ``send()`` (docs/architecture.md section 2.2) must see each reply
whole, so the adapter declares ``splits_long_messages`` and ``MAX_MESSAGE_LENGTH`` (10 752) is the size below which
the gateway does not split a reply. ``msg.out`` holds at most 2048 B of ``text``: ``send()`` cuts a longer reply to
2045 B plus ``…`` (T1.41's router replaces the cut); see ``config.MAX_MESSAGE_LENGTH``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import importlib
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aiohttp import web
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from . import gate, outbound
from .adapter_router import _FrameRouter
from .config import MAX_MESSAGE_LENGTH, PLATFORM_NAME, GlassesConfig, load, resolve_state_dir
from .edge_auth import EdgeUserRefused, edge_peer_allowed, edge_uid
from .edge_cert import edge_cert
from .edge_listener import EdgeListener, EdgeSocketError
from .envelope import FrameSchema
from .envelope_encode import encode
from .outbox import FILE_NAME, Outbox, OutboxError, Store
from .registry import Registry
from .registry_parse import RegistryError
from .registry_signer_file import DenyOverlay, LastSeen, signer_file_registry
from .revoked_ids import RevokedIds
from .server import CONNECTIONS, MAX_AUDIO_SEC, AiohttpDrift, ServerDeps, _now, _ulid, build_app, serve
from .signer_client import client_from_config, public_file
from .signer_protocol import PUBLIC_REGISTRY, SignerError

logger = logging.getLogger(__name__)

NOT_CONNECTED = "glasses: the edge listener is not running"
NOT_STORED = "glasses: the reply could not be stored (see log)"
DROPPED = "glasses adapter: inbound event dropped (allow-all gate open)"
# Worker threads of the gate's executor: one may hold a stalled recompute, the other answers the waiters.
GATE_WORKERS = 2


def _check_failed(exc: Exception) -> list[str]:
    return [f"allow-all gate check failed ({type(exc).__name__})"]


# The protocol schema the frame codec validates against: shipped inside the package (generated by tools/codegen).
ENVELOPE_SCHEMA = Path(__file__).resolve().parent / "generated" / "envelope.schema.json"
_TYPED_ERRORS = (EdgeUserRefused, EdgeSocketError, AiohttpDrift, RegistryError, SignerError, OutboxError)


@dataclass
class _Edge:
    """What ``connect()`` started: the edge listener, the one runner, and the last-seen writer."""

    listener: EdgeListener
    runner: web.AppRunner
    last_seen: LastSeen
    outbox: Outbox
    registry: Registry  # the signer's file (read-only): the recipients of send()
    revoked: RevokedIds  # the frame check (T1.248): its poll, read worker and cuts end first, in stop()

    async def stop(self) -> None:
        """End the frame check's poll, stop accepting and unlink the socket, close the app and the outbox, then flush
        last-seen off the loop (it joins). Each step runs even when an earlier one raised."""
        self.revoked.close()  # never raises
        try:
            await self.listener.stop()
        finally:
            try:
                await self.runner.cleanup()
            finally:
                try:
                    await self.outbox.close()
                finally:
                    await asyncio.to_thread(self.last_seen.close)


def _make(cfg: GlassesConfig, adapter: GlassesAdapter, state: Path) -> tuple[FrameSchema, Registry, Outbox, RevokedIds]:
    """The frame schema, the signer's registry (read-only, behind the deny overlay; the factory checks the signer
    user), the outbox and the frame check over the overlay, the outbox's purged ids and the signer's file (T1.248)."""
    jsonschema = importlib.import_module("jsonschema")  # untyped: imported by name, as in glasses.config
    schema = jsonschema.Draft202012Validator(json.loads(ENVELOPE_SCHEMA.read_text(encoding="utf-8")))
    overlay = DenyOverlay(state)
    registry = signer_file_registry(cfg, overlay)
    overflow = lambda seq: encode(outbound.overflow_error((seq, _ulid(), _now())), schema)  # noqa: E731 - one expression, read as a value
    outbox = Outbox(Store(state / FILE_NAME, time.time, overflow), adapter.router.text.dedupe.forget, registry.list)
    revoked = RevokedIds(public_file(cfg, PUBLIC_REGISTRY), registry.list, overlay.is_denied, outbox.is_revoked)
    return schema, registry, outbox, revoked


async def _start_edge(cfg: GlassesConfig, adapter: GlassesAdapter) -> _Edge:
    """The app over the signer's registry, the deny overlay and last-seen, served on ``edge.socket`` to the edge user's
    processes only, with the adapter's gate check and frame router. Raises on any start failure; nothing is left."""
    uid = edge_uid(cfg.edge_user)
    if not cfg.edge_socket:
        raise EdgeSocketError("edge.socket: not set")
    state = resolve_state_dir(cfg, os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    schema, registry, outbox, revoked = _make(cfg, adapter, state)
    certificates = edge_cert(cfg)  # the forwarded client certificate, verified against the signer's CA bundle
    last_seen = LastSeen(state)
    deps = ServerDeps(
        gate_ok_async=adapter.gate_ok_async,
        registry=registry,
        is_revoked=revoked.is_revoked,  # the overlay, the outbox's purged ids and the signer's revoked ids (T1.248)
        peer_allowed=edge_peer_allowed(uid),
        client_fingerprint=certificates.fingerprint,
        on_frame=adapter.router.on_frame,
        schema=schema,
        max_audio_sec=MAX_AUDIO_SEC,
        on_authenticated=last_seen.touch,
        signer_client=lambda: client_from_config(cfg),
        on_hello=outbox.hello,
        on_closed=outbox.closed,  # the session ended: the outbox forgets that connection
        on_revoked=outbox.on_revoked(registry.list),  # every refused revoked device is purged
    )
    listener = EdgeListener(cfg.edge_socket, uid)
    try:
        await outbox.open()  # an unusable outbox.sqlite fails the start (retryable), not every later send()
        await outbox.sweep(registry.list)  # covers a revocation written while the plugin was stopped
        await revoked.load()  # the signer's revoked ids before the first check (a failed read: checks refuse, logged)
        app = build_app(deps)
        adapter.router.bind(app[CONNECTIONS], schema, outbox)
        revoked.bind(app[CONNECTIONS].close_device, outbox.purge_soon)  # starts the poll; newly revoked: purge, 4403
        runner = await serve(app, listener)
    except BaseException:
        revoked.close()
        await outbox.close()
        await asyncio.to_thread(last_seen.close)
        raise
    return _Edge(listener, runner, last_seen, outbox, registry, revoked)


class HermesPlatformConfig(Protocol):
    """The part of Hermes' ``gateway.config.PlatformConfig`` the adapter reads (the gateway passes its own)."""

    @property
    def extra(self) -> Mapping[str, object] | None:
        """``gateway.platforms.glasses.extra`` from the Hermes config."""


class InboundEvent(Protocol):
    """The part of Hermes' ``MessageEvent`` the adapter touches (the base class handles the rest)."""

    _gateway_accepted: bool


class GlassesAdapter(BasePlatformAdapter):
    # Read by the gateway's stream/progress chunking (gateway/platforms/base.py:2038-2042).
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    # send() takes any length: cron delivery then skips its 4000-char truncation (gateway/delivery.py:248).
    splits_long_messages = True

    def __init__(self, config: HermesPlatformConfig, **_kwargs: object):
        super().__init__(config, Platform(PLATFORM_NAME))
        # validate_config() already ran; load() raises again if called with a bad config.
        self.settings = load(config.extra)
        self._gate_cache = gate.VerdictCache()
        self._logged_problems: tuple[str, ...] = ()  # the refusal this adapter last logged ((): clean)
        self._drop_logged = False
        self._log_lock = threading.Lock()
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None  # the gate's own, created by the first check
        self._pool_lock = threading.Lock()  # creation and swap of _pool (not _log_lock: it guards the log state)
        self._edge: _Edge | None = None  # the running listener, runner and last-seen writer (connect() .. disconnect())
        self._connect_lock = asyncio.Lock()  # overlapping connect() calls must not start two listeners
        self.router = _FrameRouter(self)  # the server's on_frame (T1.08)

    def gate_ok(self) -> bool:
        """False (and logged, without values) while any served profile scope opens the allow-all gate.

        The verdict is cached for one second and then always recomputed (``gate.VerdictCache``); a caller that waits
        longer than ``gate.LOCK_TIMEOUT`` for a blocked recompute is refused. The outermost adapter boundary of the
        gate check: an error the gate does not expect also refuses (fail closed) and is logged by type only, so
        ``connect()`` keeps reporting a retryable error instead of raising into the gateway. Logging: WARNING when the
        refusal differs from the one logged last, INFO once when the gate is clean again, nothing in between.
        """
        try:
            problems = self._gate_cache.violations()
        except Exception as exc:  # noqa: BLE001 - outermost adapter boundary: refuse, log the type only
            problems = _check_failed(exc)
        self._log_gate(problems)
        return not problems

    def _log_gate(self, problems: list[str]) -> None:
        seen = tuple(problems)
        with self._log_lock:
            if seen == self._logged_problems:
                return
            self._logged_problems, self._drop_logged = seen, False
        if not seen:
            logger.info("glasses adapter: allow-all gate closed again; accepting")
        for problem in problems:
            logger.warning("glasses adapter: refusing: %s", problem)

    async def gate_ok_async(self) -> bool:
        """``gate_ok()`` in the gate's own worker threads: a recompute reads files and must not block the event loop,
        and a stalled one must not occupy the loop's default executor. Fails closed: a check that cannot be submitted
        (executor shut down) refuses, and so does one that has not ended after ``gate.LOCK_TIMEOUT`` + 1 seconds (the
        caller that runs a blocked recompute itself; its worker thread ends with the read). Known gap: a logging
        filter on the ``glasses.adapter`` logger that raises escapes from the refusal paths (nothing is admitted)."""
        context = contextvars.copy_context()  # the caller's active scope reaches the worker, as with asyncio.to_thread
        try:
            future = asyncio.get_running_loop().run_in_executor(self._gate_pool(), context.run, self.gate_ok)
            # The first caller runs a stalled recompute itself (nothing else bounds it): give up like the waiters do.
            return await asyncio.wait_for(future, gate.LOCK_TIMEOUT + 1)
        except Exception as exc:  # noqa: BLE001 - outermost adapter boundary: refuse, log the type only
            self._log_gate(_check_failed(exc))
            return False

    def _gate_pool(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._pool_lock:  # several loops or threads may ask first: build exactly one pool
            if self._pool is None:
                self._pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=GATE_WORKERS, thread_name_prefix="glasses-gate"
                )
            return self._pool

    async def connect(self, *, is_reconnect: bool = False, **_kwargs: object) -> bool:
        if not await self.gate_ok_async():
            self._set_fatal_error("allow_all_gate", "glasses allow-all gate open (see log)", retryable=True)
            return False
        async with self._connect_lock:
            if self._edge is not None:
                return True  # already listening
            try:
                self._edge = await _start_edge(self.settings, self)
            except Exception as exc:  # noqa: BLE001 - any start failure is the retryable path, never raised from here
                shown = str(exc) if isinstance(exc, _TYPED_ERRORS) else type(exc).__name__  # typed texts are fixed
                logger.warning("glasses adapter: edge listener not started: %s", shown)
                self._set_fatal_error("edge_listener", f"glasses edge listener not started: {shown}", retryable=True)
                return False
        return True

    async def handle_message(self, event: InboundEvent) -> None:
        await self.hand_on(event)

    async def hand_on(self, event: InboundEvent) -> bool:
        """``handle_message``, and whether the event was handed on: False when the open allow-all gate dropped it."""
        if not await self.gate_ok_async():
            event._gateway_accepted = False  # as the base class marks an event it did not hand on
            # WARNING for the first drop of a refusal, DEBUG for the rest: the refusal itself is logged on change.
            logger.log(logging.DEBUG if self._drop_logged else logging.WARNING, DROPPED)
            self._drop_logged = True
            return False
        await super().handle_message(event)
        return True

    async def disconnect(self) -> None:
        try:
            async with self._connect_lock:  # wait for a connect() that is still starting: no listener outlives us
                edge, self._edge = self._edge, None
                if edge is not None:
                    await edge.stop()
        finally:  # the pool and the disconnected mark do not depend on the listener having stopped cleanly
            with self._pool_lock:
                pool, self._pool = self._pool, None  # recreated by the next check; a stuck worker ends with its read
            if pool is not None:
                pool.shutdown(wait=False)
            self._mark_disconnected()

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: Mapping[str, object] | None = None
    ) -> SendResult:
        lane, edge = outbound.lane_of_chat(chat_id, self.settings), self._edge
        if lane is None or edge is None:
            error = outbound.BAD_CHAT if lane is None else NOT_CONNECTED
            return SendResult(success=False, error=error, retryable=lane is not None)
        try:
            build = self.router.answer(chat_id, content, reply_to)
            stored = await edge.outbox.send(edge.registry.list, lane, build)
        except Exception as exc:  # noqa: BLE001 - a store or registry failure is a failed send, never a raise
            logger.warning("glasses adapter: reply not stored (%s)", type(exc).__name__)
            return SendResult(success=False, error=NOT_STORED, retryable=True)
        if not stored:
            return SendResult(success=False, error=outbound.NO_RECIPIENT, retryable=False)
        return SendResult(success=True)  # stored for at least one device, never "seen by the phone"

    async def get_chat_info(self, chat_id: str) -> dict[str, str]:
        return {"name": chat_id, "type": "dm"}
