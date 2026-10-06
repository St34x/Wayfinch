# SPDX-License-Identifier: Apache-2.0
"""``glasses`` platform adapter — skeleton (T1.04).

Runs inside the Hermes gateway; imports Hermes and is therefore not imported by unit tests.
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

Message length: the long-answer router in ``send()`` (docs/architecture.md section 2.2) must see each reply whole,
so ``send()`` accepts any length (``splits_long_messages``) and ``MAX_MESSAGE_LENGTH`` (10 752) is the
size below which the gateway does not split a reply -- not a frame budget: ``msg.out`` holds at most
2048 B of ``text`` and 4096 B of ``longRef.fullText``, the router cuts every reply to those budgets, and
longer replies arrive in parts (see ``config.MAX_MESSAGE_LENGTH`` for the gateway paths it controls).
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent

from . import gate
from .config import MAX_MESSAGE_LENGTH, PLATFORM_NAME, load

logger = logging.getLogger(__name__)

NOT_IMPLEMENTED = "glasses transport not implemented yet (plugin skeleton)"


class GlassesAdapter(BasePlatformAdapter):
    # Read by the gateway's stream/progress chunking (gateway/platforms/base.py:2042-2046).
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    # send() takes any length: cron delivery then skips its 4000-char truncation (gateway/delivery.py:248).
    splits_long_messages = True

    def __init__(self, config: PlatformConfig, **_kwargs: Any):
        super().__init__(config, Platform(PLATFORM_NAME))
        # validate_config() already ran; load() raises again if called with a bad config.
        self.settings = load(getattr(config, "extra", None))

    def gate_ok(self) -> bool:
        """False (and logged, without values) while any profile scope opens the allow-all gate."""
        problems = gate.violations()
        for problem in problems:
            logger.warning("glasses adapter: refusing: %s", problem)
        return not problems

    async def connect(self, *, is_reconnect: bool = False, **_kwargs: Any) -> bool:
        if not self.gate_ok():
            self._set_fatal_error("allow_all_gate", "glasses allow-all gate open (see log)", retryable=True)
            return False
        logger.warning("glasses adapter: %s; would bind %s:%d", NOT_IMPLEMENTED, self.settings.host, self.settings.port)
        self._set_fatal_error("not_implemented", NOT_IMPLEMENTED, retryable=True)
        return False

    async def handle_message(self, event: MessageEvent) -> None:
        if not self.gate_ok():
            event._gateway_accepted = False  # as the base class marks an event it did not hand on
            logger.warning("glasses adapter: inbound event dropped (allow-all gate open)")
            return None
        return await super().handle_message(event)

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict[str, Any] | None = None
    ) -> SendResult:
        return SendResult(success=False, error=NOT_IMPLEMENTED, retryable=False)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm"}
