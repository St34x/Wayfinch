# SPDX-License-Identifier: Apache-2.0
"""Read-only facts of a Hermes install for ``glasses-admin config check --live`` (``glasses.admin``).

Hermes is imported only inside ``hermes_facts``/``profile_facts``; they must run with the Hermes venv's python
after the caller has pointed ``HERMES_HOME`` at the Hermes home being checked (``hermes_home_env``) -- Hermes
resolves its home at import and on every call. Each routed profile is then read under the public
``hermes_constants.set_hermes_home_override``. The private APIs and copied behaviour used here are listed in
the T1.45 section of ``glasses.admin``'s docstring; an unexpected shape raises ``HermesContractError``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PLATFORM_NAME, GlassesConfig

DEFAULT_PROFILE = "default"
HOME_ENV = "HERMES_HOME"
DEFAULT_APPROVALS_TIMEOUT = 300  # tools/approval_context.py _get_approval_timeout at the pin
# Per-provider aliases of stt.<provider>.language (tools/transcription_cloud.py:336 at the pin).
STT_LANGUAGE_ALIASES: Mapping[str, tuple[str, ...]] = {"elevenlabs": ("language_code",)}


class HermesContractError(RuntimeError):
    """Hermes returned a shape this check does not know (names keys and types only, never values)."""


@dataclass(frozen=True)
class ProfileFacts:
    """What --live needs from one Hermes profile, resolved the way Hermes resolves it."""

    stt_provider: str
    stt_language: str
    glasses_tools: frozenset[str]
    telegram_tools: frozenset[str]
    clarify_timeout: int
    approvals_timeout: int


@dataclass(frozen=True)
class HermesFacts:
    served: frozenset[str]  # profiles this gateway serves
    routes: Mapping[str, str]  # lane -> profile of its glasses:<lane> route (absent = default)
    profiles: Mapping[str, ProfileFacts]
    gate_problems: tuple[str, ...] = ()


@contextmanager
def hermes_home_env(home: Path) -> Iterator[None]:
    """``HERMES_HOME=<home>`` in this process while the block runs (set before any Hermes import)."""
    before = os.environ.get(HOME_ENV)
    os.environ[HOME_ENV] = str(Path(home).absolute())
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(HOME_ENV, None)
        else:
            os.environ[HOME_ENV] = before


def lanes_to_route(cfg: GlassesConfig) -> tuple[str, ...]:
    """The configured lanes, or the home lane when only per-device lanes are used."""
    return cfg.lanes or ((cfg.home_lane,) if cfg.home_lane else ())


def section(config: Any, key: str) -> Mapping[str, Any]:
    """``config[key]`` as a mapping (missing/null = empty); ``HermesContractError`` for any other shape."""
    if not isinstance(config, Mapping):
        raise HermesContractError(f"the Hermes config is a {type(config).__name__}, not a mapping")
    value = config.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise HermesContractError(f"Hermes config section {key!r} is a {type(value).__name__}, not a mapping")
    return value


def approvals_timeout(config: Mapping[str, Any]) -> int:
    try:  # copy of tools/approval_context.py _get_approval_timeout (T1.45 list)
        return int(section(config, "approvals").get("timeout", DEFAULT_APPROVALS_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_APPROVALS_TIMEOUT


def stt_language(stt: Mapping[str, Any], provider: str, env_language: str = "") -> str:
    """The language hint Hermes uses (copy of tools/transcription_tools.py ``_resolve_stt_language``, T1.45
    list): first non-empty of ``stt.<provider>.language`` (+ aliases), ``stt.language`` and
    ``HERMES_LOCAL_STT_LANGUAGE`` (*env_language*); ``""`` = auto-detect."""
    per = stt.get(provider) if provider else None
    keys = ("language", *STT_LANGUAGE_ALIASES.get(provider, ()))
    candidates = [per.get(k) for k in keys] if isinstance(per, Mapping) else []
    candidates += [stt.get("language"), env_language]
    return next((c.strip() for c in candidates if isinstance(c, str) and c.strip()), "")


def _tool_names(tools: Any) -> frozenset[str]:
    if not isinstance(tools, (set, frozenset)) or not all(isinstance(t, str) for t in tools):
        raise HermesContractError(f"_get_platform_tools returned a {type(tools).__name__}, not a set of names")
    return frozenset(tools)


def profile_facts(home: Path) -> ProfileFacts:
    """One profile, read with Hermes' own loader (defaults, canonical keys, env expansion, managed overlay)."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.clarify_gateway import resolve_clarify_timeout
    from tools.transcription_common import LOCAL_STT_LANGUAGE_ENV

    token = set_hermes_home_override(home)
    try:
        config = load_config_readonly()
        stt = section(config, "stt")
        provider = str(stt.get("provider") or "")
        return ProfileFacts(
            stt_provider=provider,
            stt_language=stt_language(stt, provider, os.environ.get(LOCAL_STT_LANGUAGE_ENV, "")),
            glasses_tools=_tool_names(_get_platform_tools(config, PLATFORM_NAME)),
            telegram_tools=_tool_names(_get_platform_tools(config, "telegram")),
            clarify_timeout=resolve_clarify_timeout(config),
            approvals_timeout=approvals_timeout(config),
        )
    finally:
        reset_hermes_home_override(token)


def hermes_facts(cfg: GlassesConfig) -> HermesFacts:
    """Read-only facts from the Hermes install of ``HERMES_HOME`` (Hermes imported here only)."""
    import gateway.platforms._shared  # noqa: F401 - once loaded, glasses.gate reads every profile scope
    from gateway.config import load_gateway_config
    from gateway.profile_routing import match_profile_route
    from hermes_cli.profiles import profiles_to_serve

    from . import gate

    gw = load_gateway_config()
    served = {name: Path(home) for name, home in profiles_to_serve(bool(gw.multiplex_profiles))}
    routes: dict[str, str] = {}
    for lane in lanes_to_route(cfg):
        route = match_profile_route(gw.profile_routes or [], PLATFORM_NAME, chat_id=f"{PLATFORM_NAME}:{lane}")
        routes[lane] = route.profile if route else DEFAULT_PROFILE
    profiles = {name: profile_facts(served[name]) for name in set(routes.values()) if name in served}
    return HermesFacts(frozenset(served), routes, profiles, tuple(gate.violations()))
