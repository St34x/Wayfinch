# SPDX-License-Identifier: Apache-2.0
"""``glasses`` platform adapter — skeleton (T1.04).

Runs inside the Hermes gateway and imports Hermes; unit tests import it only against stand-in Hermes modules
(``hermes-plugin/tests/test_adapter.py``). The Hermes objects it reads are typed by small protocols below.
The WebSocket transport, device auth, outbox and the approval/clarify overrides arrive in later
tasks (T1.07 onwards). Until then the adapter fails closed: ``connect()`` reports a fatal error
and ``send()`` delivers nothing, so the gateway never believes the glasses are reachable. The
error is marked *retryable* on purpose: at the pinned Hermes a non-retryable failure of the only
platform makes the whole gateway exit (``run_startup.py`` exit 78), which would also stop cron
and every other profile. Retryable keeps the gateway up; the reconnect watcher retries with
backoff (30 s doubling, capped at 300 s).

Allow-all gate: ``connect()`` (every attempt, reconnects included) and ``handle_message()`` (every
inbound event) re-run ``glasses.gate.violations()``, so an allow-all switch or a ``*`` allowlist
added to any profile's ``.env`` after start-up refuses connections and drops events instead of
admitting everyone. The transport (T1.07) must also call ``gate_ok()`` per WebSocket connection.

Message length: the long-answer router planned for ``send()`` (docs/architecture.md section 2.2; today ``send()``
delivers nothing) must see each reply whole, so the adapter declares ``splits_long_messages`` and
``MAX_MESSAGE_LENGTH`` (10 752) is the size below which the gateway does not split a reply -- not a frame budget:
``msg.out`` holds at most 2048 B of ``text`` and 4096 B of ``longRef.fullText``, the planned router will cut every
reply to those budgets, and longer replies arrive in parts (see ``config.MAX_MESSAGE_LENGTH`` for the gateway
paths it controls).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from . import gate
from .config import MAX_MESSAGE_LENGTH, PLATFORM_NAME, load

logger = logging.getLogger(__name__)

NOT_IMPLEMENTED = "glasses transport not implemented yet (plugin skeleton)"


class HermesPlatformConfig(Protocol):
    """The part of Hermes' ``gateway.config.PlatformConfig`` the adapter reads (the gateway passes its own)."""

    @property
    def extra(self) -> Mapping[str, object] | None:
        """``gateway.platforms.glasses.extra`` from the Hermes config."""


class InboundEvent(Protocol):
    """The part of Hermes' ``MessageEvent`` the adapter touches (the base class handles the rest)."""

    _gateway_accepted: bool


class GlassesAdapter(BasePlatformAdapter):
    # Read by the gateway's stream/progress chunking (gateway/platforms/base.py:2042-2046).
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    # send() takes any length: cron delivery then skips its 4000-char truncation (gateway/delivery.py:248).
    splits_long_messages = True

    def __init__(self, config: HermesPlatformConfig, **_kwargs: object):
        super().__init__(config, Platform(PLATFORM_NAME))
        # validate_config() already ran; load() raises again if called with a bad config.
        self.settings = load(config.extra)

    def gate_ok(self) -> bool:
        """False (and logged, without values) while any profile scope opens the allow-all gate.

        The outermost adapter boundary of the gate check: an error ``gate.violations()`` does not
        expect also refuses (fail closed) and is logged by type only, so ``connect()`` keeps
        reporting a retryable error instead of raising into the gateway.
        """
        try:
            problems = gate.violations()
        except Exception as exc:  # noqa: BLE001 - outermost adapter boundary: refuse, log the type only
            problems = [f"allow-all gate check failed ({type(exc).__name__})"]
        for problem in problems:
            logger.warning("glasses adapter: refusing: %s", problem)
        return not problems

    async def connect(self, *, is_reconnect: bool = False, **_kwargs: object) -> bool:
        if not self.gate_ok():
            self._set_fatal_error("allow_all_gate", "glasses allow-all gate open (see log)", retryable=True)
            return False
        logger.warning("glasses adapter: %s; would bind %s:%d", NOT_IMPLEMENTED, self.settings.host, self.settings.port)
        self._set_fatal_error("not_implemented", NOT_IMPLEMENTED, retryable=True)
        return False

    async def handle_message(self, event: InboundEvent) -> None:
        if not self.gate_ok():
            event._gateway_accepted = False  # as the base class marks an event it did not hand on
            logger.warning("glasses adapter: inbound event dropped (allow-all gate open)")
            return
        await super().handle_message(event)

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: Mapping[str, object] | None = None
    ) -> SendResult:
        return SendResult(success=False, error=NOT_IMPLEMENTED, retryable=False)

    async def get_chat_info(self, chat_id: str) -> dict[str, str]:
        return {"name": chat_id, "type": "dm"}
