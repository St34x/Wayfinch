# SPDX-License-Identifier: Apache-2.0
"""Allow-all / wildcard gate of the ``glasses`` platform, read the way the Hermes gateway reads it.

The gateway decides per inbound event whether a sender is admitted, reading the gate variables
through ``platform_gate_env`` (``gateway/platforms/_shared.py:69-86`` at the pinned a4648c5f58):
under ``multiplex_profiles`` that is the authorising profile's secret scope, built from the
profile's own ``.env`` by ``agent.secret_scope.build_profile_secret_scope``
(``agent/secret_scope.py:391-419``, installed by ``gateway/run.py:1824-1856`` around every
authorisation check, ``gateway/run.py:4101-4106``), not ``os.environ``. A value added to any
profile's ``.env`` while the gateway runs takes effect from the next event.

So this module checks, every time it is asked:

* the process environment (single-profile gateways, the default profile's bridged values);
* the active scope via Hermes' own ``platform_gate_env`` (only when Hermes is already loaded);
* the secret scope of every installed profile via ``build_profile_secret_scope``
  (``hermes_cli/profiles.py:1173-1198`` ``profiles_to_serve(True, include_standalone=True,
  include_parked=True)``) -- broader than the served set on purpose: fail closed.

``validate_config`` calls it before the adapter is built; the adapter calls it again on every
``connect()`` (including the reconnect watcher's retries) and for every inbound event, so a
switch added to a ``.env`` later never takes effect.

Importing this module never imports Hermes: Hermes modules are used only when
``gateway.platforms._shared`` is already in ``sys.modules`` (i.e. inside the gateway process).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from .config import environ_reader, gate_violations

logger = logging.getLogger(__name__)

HERMES_MARKER_MODULE = "gateway.platforms._shared"

Reader = Callable[[str], str]


def _hermes_api() -> SimpleNamespace | None:
    """Hermes' own gate readers, or None outside a process that has already loaded Hermes."""
    shared = sys.modules.get(HERMES_MARKER_MODULE)
    if shared is None:
        return None
    from agent.secret_scope import build_profile_secret_scope
    from hermes_cli.profiles import profiles_to_serve

    return SimpleNamespace(
        platform_gate_env=shared.platform_gate_env,
        build_profile_secret_scope=build_profile_secret_scope,
        installed_profiles=lambda: profiles_to_serve(True, include_standalone=True, include_parked=True),
    )


def _mapping_reader(mapping) -> Reader:
    # Same normalisation as platform_gate_env: missing -> "", value str()-ed and stripped.
    return lambda name: "" if mapping.get(name) is None else str(mapping.get(name)).strip()


def sources() -> list[tuple[str, Reader]]:
    """``(label, reader)`` for every scope the gateway may read the gate from. Raises if Hermes is
    loaded but a profile scope cannot be read (the caller treats that as a violation)."""
    found: list[tuple[str, Reader]] = [("process environment", environ_reader())]
    api = _hermes_api()
    if api is None:
        return found
    found.append(("active scope", lambda name: api.platform_gate_env(name, "")))
    for name, home in api.installed_profiles():
        scope = api.build_profile_secret_scope(Path(home))
        found.append((f"profile {name}", _mapping_reader(scope)))
    return found


def violations() -> list[str]:
    """Every gate violation as ``"<scope>: <problem>"``; ``[]`` = the platform may run. Never raises:
    an unreadable scope is itself a violation (fail closed)."""
    problems: list[str] = []
    try:
        for label, read in sources():
            problems.extend(f"{label}: {problem}" for problem in gate_violations(read))
    except Exception as exc:
        problems.append(
            f"profile scopes unreadable ({type(exc).__name__}); refusing to run without checking the allow-all gate"
        )
    # The same problem in several scopes is reported once per scope; keep order, drop exact repeats.
    return list(dict.fromkeys(problems))
