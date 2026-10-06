# SPDX-License-Identifier: Apache-2.0
"""Hermes gateway platform plugin ``glasses`` (Wayfinch).

``register(ctx)`` registers the ``glasses`` platform with the gateway. Importing this package
and calling ``register`` must not import Hermes: the adapter module (which subclasses Hermes'
``BasePlatformAdapter``) is imported only when the gateway builds the adapter.

User allowlist: ``GLASSES_ALLOWED_USERS`` (comma-separated device ids, never ``*``). There is no
allow-all switch: ``allow_all_env`` is deliberately **not** registered, so the gateway never
consults ``GLASSES_ALLOW_ALL_USERS``; if it is set anyway (in any profile's ``.env``) the platform
is refused at build time and the adapter refuses connections and events at runtime
(``glasses.gate``).
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

from . import config as _config
from .config import ALLOWED_USERS_ENV, MAX_MESSAGE_LENGTH, PLATFORM_NAME, validate_config

logger = logging.getLogger(__name__)

__all__ = ["register", "check_requirements", "validate_config", "is_connected"]

LABEL = "Glasses"
# Libraries the plugin may use, from the Hermes venv (ENGINEERING.md section 8; jsonschema needs Hermes' `mcp`
# extra); nothing is installed.
REQUIRED_MODULES = ("aiohttp", "jsonschema", "cryptography")

PLATFORM_HINT = (
    "The user reads replies on a small smart-glasses display. Start with a one- or two-sentence "
    "answer that stands on its own. If more detail is needed, give it in full after that; the "
    "glasses show the start and forward the full text to the user's long-answer channel. Do not "
    "mention this forwarding."
)


def check_requirements() -> bool:
    """PASSIVE dependency probe (``check_fn``): importable right now? Never installs anything."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in REQUIRED_MODULES)
    except (ImportError, ValueError):
        return False


def is_connected(platform_config: Any) -> bool:
    """Configured for use: explicitly enabled in YAML and a valid config (no env auto-enable).

    Status screens call this repeatedly, so reasons are logged at DEBUG here (WARNING on build).
    """
    return bool(getattr(platform_config, "enabled", False)) and validate_config(
        platform_config, log_level=logging.DEBUG
    )


def _create_adapter(platform_config: Any):
    from .adapter import GlassesAdapter  # imports Hermes; only inside the gateway

    return GlassesAdapter(platform_config)


def register(ctx) -> None:
    """Plugin entry point called by the Hermes plugin manager."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label=LABEL,
        adapter_factory=_create_adapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[],
        install_hint="aiohttp (Hermes messaging extras), jsonschema (Hermes mcp extra), cryptography",
        is_connected=is_connected,
        allowed_users_env=ALLOWED_USERS_ENV,
        # allow_all_env deliberately absent: registering it makes the gateway honour the switch
        # per event (gateway/authz_mixin.py:645-651).
        # send_message tool chunk size (tools/send_message_tool.py:603-618); the adapter class
        # carries the same value as MAX_MESSAGE_LENGTH for the gateway's own chunking (config.py).
        max_message_length=MAX_MESSAGE_LENGTH,
        platform_hint=PLATFORM_HINT,
        emoji="\U0001f453",
        allow_update_command=False,
    )
    logger.debug("glasses platform registered (config keys: %s)", ", ".join(_config.DEFAULTS))
