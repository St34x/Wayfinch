# SPDX-License-Identifier: Apache-2.0
"""Allow-all / wildcard gate of the ``glasses`` platform, read the way the Hermes gateway reads it.

The gateway decides per inbound event whether a sender is admitted, reading the gate variables
through ``platform_gate_env`` (``gateway/platforms/_shared.py:69-86`` at the pinned d923a5eb48):
under ``multiplex_profiles`` that is the authorising profile's secret scope, built from the
profile's own ``.env`` by ``agent.secret_scope.build_profile_secret_scope``
(``agent/secret_scope.py:390-418``, installed by ``gateway/run.py:1788-1820`` around every
authorisation check, ``gateway/run.py:4037-4042``), not ``os.environ``. A value added to any
profile's ``.env`` while the gateway runs takes effect from the next event.

So this module checks, every time it is asked:

* the process environment (single-profile gateways, the default profile's bridged values);
* the active scope via Hermes' own ``platform_gate_env`` (only when Hermes is already loaded);
* the secret scope of every profile this process serves via ``build_profile_secret_scope``
  (``hermes_cli/profiles.py:1135-1160`` ``profiles_to_serve(True)``): only those can authorise a
  glasses event here. A parked or standalone profile is not read (``glasses-admin config check --live``
  asks the same question and only adds the safety of every installed profile's ``.env`` file).

``validate_config`` calls ``violations()`` before the adapter is built; the adapter asks again on
every ``connect()`` (including the reconnect watcher's retries) and for every inbound event, so a
switch added to a ``.env`` later never takes effect. Those calls go through ``VerdictCache``: a verdict
is reused for ``CACHE_TTL`` (one second, measured with a monotonic clock) and then always recomputed, whether or not
a file changed (the scope also holds sources that have no cheap stamp: the administrator-managed ``.env``, the
external-secret snapshot, ``config.yaml`` ``secrets:``). A connection or a frame inside that second costs no file
read. A changed process switch, or a different active scope in the caller's context, is seen at once. A recompute
that blocks holds the cache's lock: other callers wait at most ``LOCK_TIMEOUT`` seconds and are refused, or at once
once the recompute has run that long; the caller that runs it is bounded by the adapter, not by this module.

Importing this module never imports Hermes: Hermes modules are used only when
``gateway.platforms._shared`` is already in ``sys.modules`` (i.e. inside the gateway process).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .config import environ_reader, gate_violations
from .config_access import ALLOW_ALL_ENV, ALLOWED_USERS_ENV, GATEWAY_ALLOW_ALL_ENV, GATEWAY_ALLOWED_USERS_ENV

logger = logging.getLogger(__name__)

HERMES_MARKER_MODULE = "gateway.platforms._shared"

# How long a verdict is reused (monotonic seconds); also the longest a switch added to any scope source goes unseen.
# After that it is recomputed unconditionally: nothing renews a verdict.
CACHE_TTL = 1.0
# The longest a caller waits for the lock while another caller's recompute runs (a read that never returns holds it);
# then the caller is refused with LOCK_TIMEOUT_REFUSAL (no value in the text).
LOCK_TIMEOUT = 2.0
LOCK_TIMEOUT_REFUSAL = "allow-all gate check timed out; refusing"
# Appended to a problem found in a profile's scope: the switch lives in that profile's .env.
PROFILE_HINT = "; or remove the switch from that profile"
SWITCHES = (ALLOW_ALL_ENV, ALLOWED_USERS_ENV, GATEWAY_ALLOWED_USERS_ENV, GATEWAY_ALLOW_ALL_ENV)

Reader = Callable[[str], str]

# What an unreadable profile scope or a drifted Hermes API raises: file and parse errors, a missing key, a missing or
# renamed Hermes module/name, a changed signature. violations() turns these into a violation (fail closed).
SCOPE_ERRORS = (OSError, ValueError, LookupError, ImportError, AttributeError, TypeError, RuntimeError)


@dataclass(frozen=True)
class HermesGateApi:
    """Hermes' own gate readers, as the plugin calls them.

    ``served_profiles`` lists the profiles this process serves (``profiles_to_serve(True)``), the only ones whose
    scope can authorise a glasses event.
    """

    platform_gate_env: Callable[[str, str], str]
    build_profile_secret_scope: Callable[[Path], Mapping[str, object]]
    served_profiles: Callable[[], Iterable[tuple[str, str | os.PathLike[str]]]]


def _hermes_api() -> HermesGateApi | None:
    """Hermes' own gate readers, or None outside a process that has already loaded Hermes."""
    shared = sys.modules.get(HERMES_MARKER_MODULE)
    if shared is None:
        return None
    from agent.secret_scope import build_profile_secret_scope
    from hermes_cli.profiles import profiles_to_serve

    return HermesGateApi(
        platform_gate_env=shared.platform_gate_env,
        build_profile_secret_scope=build_profile_secret_scope,
        served_profiles=lambda: profiles_to_serve(True),
    )


def _mapping_reader(mapping: Mapping[str, object]) -> Reader:
    # Same normalisation as platform_gate_env: missing -> "", value str()-ed and stripped.
    return lambda name: "" if mapping.get(name) is None else str(mapping.get(name)).strip()


def sources() -> list[tuple[str, Reader]]:
    """``(label, reader)`` for every scope the gateway may read the gate from in this process. Raises if Hermes is
    loaded but a profile scope cannot be read (the caller treats that as a violation)."""
    found: list[tuple[str, Reader]] = [("process environment", environ_reader())]
    api = _hermes_api()
    if api is None:
        return found
    found.append(("active scope", lambda name: api.platform_gate_env(name, "")))
    for name, home in api.served_profiles():
        scope = api.build_profile_secret_scope(Path(home))
        found.append((f"profile {name}", _mapping_reader(scope)))
    return found


def violations() -> list[str]:
    """Every gate violation as ``"<scope>: <problem>"``; ``[]`` = the platform may run. An unreadable
    scope (any of ``SCOPE_ERRORS``) is itself a violation (fail closed). Any other exception is a bug
    and propagates; every caller refuses on it too (``validate_config``, the adapter's ``gate_ok``,
    the ``config check`` CLI)."""
    problems: list[str] = []
    try:
        for label, read in sources():
            hint = PROFILE_HINT if label.startswith("profile ") else ""
            problems.extend(f"{label}: {problem}{hint}" for problem in gate_violations(read))
    except SCOPE_ERRORS as exc:
        problems.append(_unreadable(exc))
    # The same problem in several scopes is reported once per scope; keep order, drop exact repeats.
    return list(dict.fromkeys(problems))


def _unreadable(exc: Exception) -> str:
    return f"profile scopes unreadable ({type(exc).__name__}); refusing to run without checking the allow-all gate"


Key = tuple[tuple[str, ...], tuple[str, ...]]


def _cache_key() -> Key:
    """The four switches as the process holds them and as ``platform_gate_env`` returns them in the CALLER's context
    (the active scope is a context variable: a turn-prepared scope differs per caller). No file is read. Raises like
    ``sources()`` does (the caller treats ``SCOPE_ERRORS`` as a violation)."""
    read = environ_reader()
    process = [read(name) for name in SWITCHES]
    api = _hermes_api()
    # List comprehensions, not generators: a StopIteration out of a Hermes reader must not turn into a RuntimeError.
    scope = [] if api is None else [api.platform_gate_env(name, "") for name in SWITCHES]
    return tuple(process), tuple(scope)


@dataclass(frozen=True)
class _Verdict:
    key: Key = field(repr=False)  # the switch values as read: never in a repr, a log line or a traceback
    at: float
    problems: tuple[str, ...]


class VerdictCache:
    """``violations()`` of the gate, reused for at most ``ttl`` seconds.

    A verdict, clean or not, is valid for ``ttl`` seconds of a monotonic clock and is then recomputed from scratch:
    nothing renews it. The gateway's scope reads more than files that can be stamped cheaply (the administrator-managed
    ``.env``, the external-secret snapshot, ``config.yaml`` ``secrets:``), so no stamp decides that a verdict is still
    good. The key of a verdict is the four switches in the process environment plus the same four as the CALLER's
    active scope returns them (a few dict reads, only when Hermes is loaded); both are read on every call, inside the
    lock, and a change of either recomputes at once. The lock is held across a recompute, so concurrent callers share
    one. A recompute that blocks (a ``.env`` that is a FIFO, a hung network home) therefore holds the lock: every other
    caller waits at most ``LOCK_TIMEOUT`` seconds and is then refused with ``LOCK_TIMEOUT_REFUSAL``, never admitted;
    once the recompute has run longer than that, new callers are refused at once without waiting;
    nothing is stored for them and they do not recompute. The recompute that holds the lock continues, and the next call
    after it ends is judged normally. The caller that drives the blocked recompute is not bounded here (it runs
    ``compute`` itself); the adapter bounds it (``GlassesAdapter.gate_ok_async``). Safe to call from several threads.
    """

    def __init__(
        self,
        ttl: float = CACHE_TTL,
        clock: Callable[[], float] | None = None,
        compute: Callable[[], list[str]] | None = None,
        lock_timeout: float | None = None,
    ) -> None:
        self._ttl = ttl
        self._clock = clock or time.monotonic  # looked up here, not at import: tests swap the module's ``time``
        self._compute = compute or violations
        self._lock_timeout = LOCK_TIMEOUT if lock_timeout is None else lock_timeout
        self._lock = threading.Lock()
        self._verdict: _Verdict | None = None
        self._started: float | None = None  # clock reading at which the running recompute began; None: none running

    def violations(self) -> list[str]:
        if self._stalled():
            return [LOCK_TIMEOUT_REFUSAL]  # the recompute has outlived the wait: refuse at once, take no thread's time
        if not self._lock.acquire(timeout=self._lock_timeout):
            return [LOCK_TIMEOUT_REFUSAL]
        try:
            return self._locked_violations()
        finally:
            self._lock.release()

    def _stalled(self) -> bool:
        started = self._started
        return started is not None and self._clock() - started > self._lock_timeout

    def _locked_violations(self) -> list[str]:
        try:
            key = _cache_key()
        except SCOPE_ERRORS as exc:
            self._verdict = None  # a refusal that is not stored: the next call reads the scope again
            return [_unreadable(exc)]
        now, held = self._clock(), self._verdict
        # A clock reading before the verdict's own (a clock that is not monotonic) counts as expired.
        if held is not None and held.key == key and 0 <= now - held.at < self._ttl:
            return list(held.problems)
        self._verdict = None  # an error out of the recompute below leaves no verdict to serve
        self._started = now
        try:
            problems = tuple(self._compute())
        finally:
            self._started = None
        self._verdict = _Verdict(key, now, problems)
        return list(problems)
