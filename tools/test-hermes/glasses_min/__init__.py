# SPDX-License-Identifier: Apache-2.0
"""Test harness: minimal `glasses` platform plugin (registration only; adapter in adapter.py)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PLATFORM_HINT = "You are talking to the operator through smart glasses. Answer in at most two short sentences."


def check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401 - already in the Hermes venv via [messaging]
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    return bool((getattr(config, "extra", None) or {}).get("port")) or bool(getattr(config, "enabled", False))


def register(ctx) -> None:
    from .adapter import GlassesAdapter

    ctx.register_platform(
        name="glasses",
        label="Glasses",
        adapter_factory=lambda cfg: GlassesAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint="aiohttp (Hermes [messaging] extra)",
        # allow_all_env is harness-only: the production plugin (hermes-plugin/glasses) deliberately does not
        # register it, so no allow-all switch can open a real device to every user.
        allowed_users_env="GLASSES_ALLOWED_USERS",
        allow_all_env="GLASSES_ALLOW_ALL_USERS",
        cron_deliver_env_var="GLASSES_HOME_CHANNEL",
        max_message_length=2000,
        platform_hint=_PLATFORM_HINT,
        emoji="\U0001f453",
        allow_update_command=False,
    )
