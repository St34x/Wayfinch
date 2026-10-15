# SPDX-License-Identifier: Apache-2.0
"""Facts of a Hermes install for ``glasses-admin config check --live`` (``glasses.admin``), read without writing
anything into the Hermes home.

Hermes is imported only inside the functions of ``hermes_facts`` and ``hermes_env.read_env_file``; they must run
with the Hermes venv's python
after the caller has pointed ``HERMES_HOME`` at the Hermes home being checked (``hermes_home_env``). The
env-dependent facts see ``<home>/.env`` (``glasses.hermes_env``). Hermes'
own loaders create the home skeleton and a ``config.yaml.good`` backup, and importing ``hermes_cli.config``
already runs a config load: so Hermes is imported under a throwaway home (``quarantined_import``), and the
configs are read by ``read_gateway_config``/``read_profile_config``, which rebuild what the loaders return from
their pure helpers. Each routed profile is read under the public ``hermes_constants.set_hermes_home_override``.
An unexpected shape raises ``HermesContractError``.

T1.45 contract-test list -- private Hermes APIs and copied Hermes behaviour (pinned a4648c5f58), each to
be pinned by a contract test so a Hermes bump fails loudly instead of drifting:

* ``hermes_cli.tools_config._get_platform_tools(config, platform) -> set[str]`` (private; design D5a).
* ``tools.transcription_tools._resolve_stt_language`` -- copied in ``hermes_facts.stt_language``:
  ``stt.<provider>.language`` (+ ``language_code`` for elevenlabs, tools/transcription_cloud.py:336) >
  ``stt.language`` > env ``tools.transcription_common.LOCAL_STT_LANGUAGE_ENV`` (imported, not copied).
* ``tools.approval_context._get_approval_timeout`` -- copied in ``hermes_facts.approvals_timeout``:
  ``int(approvals.timeout)``, 300 when missing or not a number (its upper clamp does not affect ``<= 0``).
* ``gateway.config._coerce_bool`` (private) -- copied in ``hermes_facts.gateway_enabled``, with ``_TRUTHY_STRINGS``
  (``1``/``true``/``yes``/``on``) from ``gateway/config.py``: how ``PlatformConfig.from_dict`` reads ``enabled``.
* ``gateway.platforms._shared`` -- imported for its side effect: ``glasses.gate`` then reads every profile.
* ``glasses.hermes_merge.platform_view`` -- copy of ``gateway.config_loader.merge_platform_sections``,
  ``PlatformConfig.from_dict`` (private ``PlatformConfig._TYPED_KEYS``) and the root-block bridge
  (``_bridged_keys``); differential test in tests/test_config_check_merge.py.
* ``hermes_facts.read_profile_config`` -- copy of the pure steps of ``hermes_cli.config._load_config_impl``
  (``load_config_readonly`` writes the home skeleton and a backup): ``DEFAULT_CONFIG`` deep-merged
  (``_deep_merge``) with ``<home>/config.yaml``, ``_canonicalize_config``, ``_expand_env_vars``,
  ``_merge_managed_overlay`` (all private). Its ``max_turns`` relocation and last-known-good fallback are not
  copied (no fact here depends on them); a ``config.yaml`` that does not parse raises.
* ``hermes_facts.read_gateway_config`` -- copy of the layers of ``gateway.config.load_gateway_config`` that
  carry ``multiplex_profiles`` and ``profile_routes``: ``gateway.config_loader.load_legacy_gateway_json``,
  ``read_yaml_layers``, ``bridge_toplevel_keys`` and ``GatewayConfig.from_dict``; its plugin discovery and
  platform env overrides are not copied (discovery loads the config, which writes).
* ``hermes_facts.read_allow_all_bridge`` -- copy of the read in ``gateway.config_loader.bridge_core_env_settings``
  (private): ``allow_all_users`` at the top level of ``config.yaml``, else ``gateway.allow_all_users``, true
  for ``true``/``1``/``yes`` (any case) while ``GATEWAY_ALLOW_ALL_USERS`` is unset, becomes
  ``GATEWAY_ALLOW_ALL_USERS=true`` in ``os.environ`` and ``gateway.config_loader._BRIDGED_ALLOW_ALL_USERS`` (private
  module flag; the default profile's secret scope reads the grant through it) around ``glasses.gate.violations``
  (``allow_all_bridged``), so the gate sees what the gateway sees. The layers come from
  ``gateway.config_loader.read_yaml_layers``. Without it a config granting every sender access passed the check.
  Tests: tests/test_config_check_allow_all.py.
* ``hermes_facts.known_toolsets`` -- ``toolsets.TOOLSETS`` and ``hermes_cli.tools_config.CONFIGURABLE_TOOLSETS``
  (module-level tables): the only tool names a message shows.
* ``hermes_facts.quarantined_import`` -- relies on importing ``hermes_cli.config`` running a config load
  (``_inject_profile_env_vars``) under the home the context resolves; the throwaway home swallows it. The
  ``_get_platform_tools`` call (plugin discovery loads a config too) runs under it as well, so plugins
  installed under a profile's own home are not discovered by this check.
* ``hermes_env.read_env_file`` / ``environ_with`` -- ``agent.secret_scope.load_env_file`` (Hermes' ``.env``
  tokenizer; read only, never touches ``os.environ``) replaces ``hermes_cli.env_loader.load_hermes_dotenv``,
  which sanitises and rewrites the file. The file must be a regular file of at most 1 MiB; names that load code
  or move directories (``PATH``, ``LD_*``, ``PYTHON*``, ``HOME``, ``TMPDIR``, ``HERMES_BUNDLED_PLUGINS``,
  ``HERMES_ENABLE_PROJECT_PLUGINS``, ``HERMES_MANAGED_DIR``, ``HERMES_PLUGINS_*`` ...) are not overlaid.
  ``hermes_facts`` overlays ``<home>/.env`` on the process environment around ``read_gateway_config``, the allow-all
  gate and each
  profile's ``read_profile_config`` only (never around Hermes' plugin discovery: a ``.env`` cannot make a plugin run
  here), and each profile read overlays that profile's own ``.env`` on top for its ``${VAR}`` refs (the gateway
  resolves a routed profile's refs against its secret scope; a ref the scope lacks stays unresolved there, here it
  falls back to the environment). ``HERMES_LOCAL_STT_LANGUAGE`` comes from the launch ``.env``, else the launch
  environment. Not loaded: ``.op.env``, the managed ``.env``, external secret sources. The environment is
  restored afterwards. A value of 4+ characters is masked in the output (``hermes_env.mask_values``); a shorter
  one or a process-environment value is hidden as a whole route name (``name_unless_from_env``) only.
* ``hermes_env.silenced_hermes`` -- ``logging.disable`` and a warnings filter around every Hermes call: Hermes
  logs quote config values (e.g. ``Skipping profile route ...: invalid profile name '<value>'``).
* Shapes asserted at run time (``HermesContractError``, a clear error, never a silent default):
  the profile config is a mapping, its ``stt``/``approvals`` are mappings or null,
  ``_get_platform_tools`` returns a set of names.

Public Hermes APIs used instead of copies: ``hermes_constants.set/reset_hermes_home_override`` and
``get_hermes_home``, ``tools.clarify_gateway.resolve_clarify_timeout``,
``gateway.profile_routing.match_profile_route``, ``hermes_cli.profiles.profiles_to_serve``.
"""

from __future__ import annotations

import copy
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from .config import PLATFORM_NAME, GlassesConfig
from .hermes_env import (  # noqa: F401 - re-exported: admin and the tests use hermes_facts.environ_with etc.
    HOME_ENV,
    MAX_ENV_FILE_BYTES,
    allow_all_bridge,
    env_file_is_unsafe,
    environ_with,
    hermes_home_env,
    name_unless_from_env,
    read_env_file,
    secret_values,
    silenced_hermes,
)

DEFAULT_PROFILE = "default"
BRIDGED_FLAG = "_BRIDGED_ALLOW_ALL_USERS"  # gateway.config_loader at the pin
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
    known_toolsets: frozenset[str] = frozenset()  # the only tool names a message may show (anything else: a count)
    # every overlaid .env value of four or more characters (``hermes_env.mask_values`` hides them)
    env_values: frozenset[str] = frozenset()


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


GATEWAY_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})  # gateway/config.py _TRUTHY_STRINGS at the pin


def gateway_enabled(value: Any) -> bool:
    """Whether the gateway starts a platform whose ``enabled`` is *value* (copy of ``gateway.config._coerce_bool``
    with default ``False``, T1.45 list): absent = no; a string is true only for ``1``/``true``/``yes``/``on``
    (any case, stripped; the falsy and unrecognised tokens are false); any other value is its truthiness."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in GATEWAY_TRUE_STRINGS
    return bool(value)


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


@contextmanager
def quarantined_import() -> Iterator[None]:
    """Run the block with a throwaway Hermes home (public ``set_hermes_home_override``).

    Importing ``hermes_cli.config`` (and so ``hermes_cli.tools_config``, ``gateway.config``) runs a config load,
    which creates the home skeleton and a backup in the home it resolves; under this override it does that in
    a temporary directory that is removed afterwards, never in the Hermes home being checked.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    with tempfile.TemporaryDirectory(prefix="glasses-admin-") as scratch:
        token = set_hermes_home_override(scratch)
        try:
            yield
        finally:
            reset_hermes_home_override(token)


def read_profile_config(home: Path) -> dict[str, Any]:
    """The config of the profile at *home* as ``hermes_cli.config.load_config_readonly`` builds it, minus its
    writes: ``DEFAULT_CONFIG`` deep-merged with ``<home>/config.yaml``, canonical keys, ``${VAR}`` expansion,
    managed overlay (copy of ``_load_config_impl``'s pure steps, T1.45 list). A file that does not parse raises.
    """
    import yaml
    from hermes_cli.config import (
        DEFAULT_CONFIG,
        _canonicalize_config,
        _deep_merge,
        _expand_env_vars,
        _merge_managed_overlay,
    )

    path = Path(home) / "config.yaml"
    user = yaml.safe_load(path.read_text(encoding="utf-8-sig")) if path.exists() else None
    if user is not None and not isinstance(user, dict):
        raise HermesContractError(f"a Hermes config.yaml is a {type(user).__name__}, not a mapping")
    merged = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), user or {})
    expanded, _managed = _merge_managed_overlay(_expand_env_vars(_canonicalize_config(merged)))
    return dict(expanded)


def read_gateway_config(home: Path) -> Any:
    """``gateway.config.load_gateway_config()`` for the data the check needs (``multiplex_profiles``,
    ``profile_routes``), minus its plugin discovery and config loads, which write (T1.45 list): the legacy
    ``gateway.json`` layer, the ``config.yaml`` layer with the managed overlay, the top-level key bridge."""
    from gateway import config_loader
    from gateway.config import GatewayConfig

    data = config_loader.load_legacy_gateway_json(Path(home))
    layers = config_loader.read_yaml_layers(Path(home))
    if layers:
        config_loader.bridge_toplevel_keys(layers, layers.get("gateway"), data)
    return GatewayConfig.from_dict(data)


def read_allow_all_bridge(home: Path) -> dict[str, str]:
    """The ``GATEWAY_ALLOW_ALL_USERS`` Hermes' bridge exports for ``allow_all_users`` of ``config.yaml`` (T1.45
    list): ``{}`` or ``{"GATEWAY_ALLOW_ALL_USERS": "true"}``. Run it inside the launch ``.env`` overlay."""
    from gateway import config_loader

    return allow_all_bridge(config_loader.read_yaml_layers(Path(home)))


@contextmanager
def allow_all_bridged(bridge: Mapping[str, str]) -> Iterator[None]:
    """What Hermes' bridge leaves behind at gateway start while the block runs: the environment variable and
    ``gateway.config_loader._BRIDGED_ALLOW_ALL_USERS`` (private; the default profile's secret scope seeds the grant
    from it, ``agent.secret_scope.build_profile_secret_scope``). Nothing when *bridge* is empty; restored."""
    from gateway import config_loader

    missing = object()
    before = getattr(config_loader, BRIDGED_FLAG, missing)
    with environ_with(bridge):
        try:
            if bridge:
                setattr(config_loader, BRIDGED_FLAG, True)
            yield
        finally:
            if bridge and before is missing:
                delattr(config_loader, BRIDGED_FLAG)
            elif bridge:
                setattr(config_loader, BRIDGED_FLAG, before)


def unsafe_env_problems(installed: Callable[[], Iterable[tuple[str, str | os.PathLike[str]]]]) -> tuple[str, ...]:
    """Gate problems for the installed profiles (*installed*: the list ``glasses.gate`` reads) whose ``.env`` is
    unsafe to open. Hermes' own reader would open it (a FIFO blocks for ever), so the gate is not run: it could
    not be checked. A profile list that cannot be read is left to the gate, which reports that itself."""
    from .gate import SCOPE_ERRORS

    try:
        listed = list(installed())
    except SCOPE_ERRORS:
        return ()
    return tuple(
        f"profile {name}: .env is not a regular file of at most {MAX_ENV_FILE_BYTES} bytes - "
        "the allow-all gate was not checked, fix or remove it"
        for name, path in listed
        if env_file_is_unsafe(Path(path))
    )


def known_toolsets() -> frozenset[str]:
    """The tool names a message may show: Hermes' static toolset table, the configurable ones and this plugin's own
    platform toolset (T1.45 list). A name from a config file that is none of them is only counted."""
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS

    table = import_module("toolsets").TOOLSETS  # a top-level Hermes module: by name, the dev venv has no stub
    return frozenset(table) | {key for key, _label, _desc in CONFIGURABLE_TOOLSETS} | {f"hermes-{PLATFORM_NAME}"}


def profile_facts(home: Path, launch_env: Mapping[str, str] | None = None) -> ProfileFacts:
    """One profile, read the way Hermes reads it (defaults, canonical keys, env expansion, managed overlay).

    The whole read runs under a throwaway Hermes home: ``_get_platform_tools`` discovers plugins, which loads
    the config of the home it resolves (and creates its skeleton); the profile's own files are read by path.
    Plugins installed under the profile's home are therefore not discovered (T1.45 list). ``${VAR}`` in the
    profile's config.yaml sees the process environment overlaid with *launch_env* (the launch home's ``.env``,
    see ``hermes_facts``) and the profile's own ``.env``: the gateway resolves a routed profile's refs against
    that profile's secret scope. The overlay covers the config read only, never the plugin discovery.
    """
    launch_env = launch_env or {}
    with quarantined_import():
        from hermes_cli.tools_config import _get_platform_tools
        from tools.clarify_gateway import resolve_clarify_timeout
        from tools.transcription_common import LOCAL_STT_LANGUAGE_ENV

        env_language = launch_env.get(LOCAL_STT_LANGUAGE_ENV, os.environ.get(LOCAL_STT_LANGUAGE_ENV, ""))
        with environ_with({**launch_env, **read_env_file(home)}):
            config = read_profile_config(home)
        stt = section(config, "stt")
        provider = str(stt.get("provider") or "")
        return ProfileFacts(
            stt_provider=provider,
            stt_language=stt_language(stt, provider, env_language),
            glasses_tools=_tool_names(_get_platform_tools(config, PLATFORM_NAME)),
            telegram_tools=_tool_names(_get_platform_tools(config, "telegram")),
            clarify_timeout=resolve_clarify_timeout(config),
            approvals_timeout=approvals_timeout(config),
        )


def hermes_facts(cfg: GlassesConfig) -> HermesFacts:
    """Facts from the Hermes install of ``HERMES_HOME``, read without writing (Hermes imported here only).

    Env-dependent facts (``${VAR}`` in config.yaml, ``HERMES_LOCAL_STT_LANGUAGE``, the ``allow_all_users``
    bridge) are evaluated against the environment the gateway sees: the process environment overlaid with
    ``<home>/.env`` (the gateway's startup load overrides the shell with it), around the pure config reads and the
    allow-all gate only, restored when each returns. Hermes' log records and warnings are silenced: they quote
    config values."""
    with silenced_hermes():
        return _hermes_facts(cfg)


def _hermes_facts(cfg: GlassesConfig) -> HermesFacts:
    with quarantined_import():
        import agent.secret_scope  # noqa: F401 - glasses.gate reads every profile scope through it
        import gateway.config_loader  # noqa: F401 - imported here, read_gateway_config needs it
        import gateway.platforms._shared  # noqa: F401 - once loaded, glasses.gate reads every profile scope
        from gateway.profile_routing import match_profile_route
        from hermes_cli.profiles import profiles_to_serve

        known = known_toolsets()
    from hermes_constants import get_hermes_home

    from . import gate

    home = get_hermes_home()
    launch_env = read_env_file(home)
    with environ_with(launch_env):
        gw = read_gateway_config(home)
        bridge = read_allow_all_bridge(home)
    served = {name: Path(path) for name, path in profiles_to_serve(bool(gw.multiplex_profiles))}
    served_envs = [read_env_file(path) for path in served.values()]
    routes: dict[str, str] = {}
    for lane in lanes_to_route(cfg):
        route = match_profile_route(gw.profile_routes or [], PLATFORM_NAME, chat_id=f"{PLATFORM_NAME}:{lane}")
        name = route.profile if route else DEFAULT_PROFILE
        routes[lane] = name_unless_from_env(name, served, launch_env, *served_envs, os.environ)
    profiles = {name: profile_facts(served[name], launch_env) for name in set(routes.values()) if name in served}
    secrets = secret_values(launch_env, *served_envs)
    unsafe = unsafe_env_problems(lambda: profiles_to_serve(True, include_standalone=True, include_parked=True))
    with environ_with(launch_env), allow_all_bridged(bridge):  # the gate also reads every profile's .env itself
        problems = unsafe or tuple(gate.violations())
    return HermesFacts(frozenset(served), routes, profiles, problems, known, secrets)
