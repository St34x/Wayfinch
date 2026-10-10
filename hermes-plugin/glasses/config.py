# SPDX-License-Identifier: Apache-2.0
"""Configuration of the `glasses` platform plugin.

The settings live in the Hermes config under ``gateway.platforms.glasses.extra``. Keys, types, bounds,
defaults and the user-facing messages are defined once in ``config.schema.json`` (JSON Schema 2020-12,
ENGINEERING.md section 4). ``check()`` validates with it (``jsonschema``, imported lazily; a declared plugin dependency,
see docs/dependencies.md -- Hermes itself ships it only with its ``mcp`` extra; when it is missing ``check()`` raises
``DependencyMissing`` naming the module: one log line in the gateway, exit 2 in ``config check``) and
adds the rules a schema cannot express (``glasses.config_values``):

* ``host`` is a loopback IP literal (hostnames, ``localhost`` included, are refused);
* ``public_url`` is empty or a strict ``wss://`` URL, stored in normal form;
* ``state_dir`` and ``edge.tls_dir`` are absolute paths without surrounding whitespace or control
  characters (or empty);
* ``bridge_port`` differs from ``port``; ``home_lane`` is one of ``lanes`` unless ``lanes`` is ``[]``.

Every problem is reported, one message per path: ``<path>: <problem> (got <value>) — <hint>``; callers
print it as ``glasses.<message>``. A string value is shown only for keys the schema marks ``x-echo``
(names, enum values, paths) and never when it contains ``@`` or ``://``; never for URL- or
credential-capable keys (``public_url``, ``meeting.endpoint``/``provider``/``llm.*``) or for unknown or
refused keys. An unknown key is named only when its name looks like a key name (1-32 characters, an ASCII letter
first, then letters, ``_`` and ``-``, at most 2 digits); any other name is ``<an unknown key of N characters>``.
Patterns are anchored and guarded against a trailing newline (Python ``$``); code uses ``re.fullmatch`` only.

Unknown keys are reported as warnings, not errors -- except the access-control keys the Hermes
gateway itself acts on for any platform (``HERMES_AUTH_KEYS``): those are refused, because the
glasses platform admits only enrolled devices listed in ``GLASSES_ALLOWED_USERS``. Keys the gateway
injects (``HERMES_EXTRA_KEYS``) are skipped silently. No Hermes import: unit-testable and reused by
the admin CLI (``glasses.admin config check``).

This module is the shell: it reads the schema file, wraps ``jsonschema``, reads ``os.environ`` and logs. The pure
parts live in ``glasses.config_core`` (names, limits, types, messages), ``glasses.config_access`` (allow-all and
access-control rules) and ``glasses.config_values`` (value rules); the names other modules use are re-exported here.
"""

from __future__ import annotations

import functools
import importlib
import json
import logging
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol

from .config_access import ALLOW_ALL_ENV, auth_key_message
from .config_access import ALLOWED_USERS_ENV as ALLOWED_USERS_ENV
from .config_access import HERMES_AUTH_KEYS as HERMES_AUTH_KEYS
from .config_access import gate_violations as gate_violations
from .config_access import parse_allowlist as parse_allowlist
from .config_core import DEFAULTS as DEFAULTS
from .config_core import ECHO_MAX_CHARS as ECHO_MAX_CHARS
from .config_core import LANE_RE as LANE_RE
from .config_core import LONG_FULLTEXT_MAX_BYTES as LONG_FULLTEXT_MAX_BYTES
from .config_core import MAX_LANES as MAX_LANES
from .config_core import MAX_LONG_THRESHOLD as MAX_LONG_THRESHOLD
from .config_core import MSG_OUT_TEXT_LONGREF_MAX_BYTES as MSG_OUT_TEXT_LONGREF_MAX_BYTES
from .config_core import MSG_OUT_TEXT_MAX_BYTES as MSG_OUT_TEXT_MAX_BYTES
from .config_core import PLATFORM_NAME as PLATFORM_NAME
from .config_core import PLATFORM_RE as PLATFORM_RE
from .config_core import CheckResult as CheckResult
from .config_core import ConfigError as ConfigError
from .config_core import DependencyMissing as DependencyMissing
from .config_core import GlassesConfig as GlassesConfig
from .config_core import describe as describe
from .config_core import extra_of as extra_of
from .config_core import is_long as is_long
from .config_core import may_hold_credentials as may_hold_credentials
from .config_core import message as message
from .config_core import path_str
from .config_values import build, prune, semantic_errors
from .config_values import is_loopback_host as is_loopback_host
from .config_values import normalise_public_url as normalise_public_url

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("config.schema.json")

# Gateway-side message cap. The planned long-answer router (docs/architecture.md section 2.2; today `send()` is a
# stub) must see every reply whole, so the gateway must not split it first. At the pinned Hermes the gateway splits by
#   * the adapter class attribute ``MAX_MESSAGE_LENGTH`` (default 4096) for streamed replies
#     (gateway/stream_consumer_fallback.py:211-222, 378-395 via
#     gateway/platforms/base.py:2042-2046) and tool-progress bubbles (gateway/run_turn_runner.py:549-561);
#   * ``PlatformEntry.max_message_length`` (0 = no split) in the send_message tool
#     (tools/send_message_tool.py:603-618, 703-704);
#   * a 4000-char truncation of cron output unless the adapter sets ``splits_long_messages``
#     (gateway/delivery.py:235-258).
# The adapter sets ``splits_long_messages = True`` and both caps to MAX_MESSAGE_LENGTH: the reply size
# (characters) below which the gateway does not split a reply. It is not a frame budget: no reply above
# MSG_OUT_TEXT_MAX_BYTES fits a msg.out (``text`` <= 2048 B, ``longRef.fullText`` <= 4096 B), so the
# planned long-answer router (T1.41) will cut every reply to those budgets; replies above this cap arrive in parts.
# The value is the 64 KiB frame cap minus 1 KiB envelope headroom over 6 bytes per character (the longest
# JSON escape, ``\u00XX``). One constant serves the registry field and the class attribute.
FRAME_MAX_BYTES = 65_536
ENVELOPE_HEADROOM_BYTES = 1_024
JSON_MAX_BYTES_PER_CHAR = 6
MAX_MESSAGE_LENGTH = (FRAME_MAX_BYTES - ENVELOPE_HEADROOM_BYTES) // JSON_MAX_BYTES_PER_CHAR  # 10 752


@functools.lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    """The parsed ``config.schema.json`` (read once)."""
    loaded: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return loaded


# --------------------------------------------------------------------------------------------
# jsonschema behind a typed wrapper: the plugin uses one validator class and four error attributes
# --------------------------------------------------------------------------------------------


class _SchemaError(Protocol):
    @property
    def absolute_path(self) -> Iterable[str | int]: ...
    @property
    def schema(self) -> Mapping[str, Any]: ...
    @property
    def instance(self) -> Any: ...
    @property
    def validator(self) -> object: ...


class _Validator(Protocol):
    def iter_errors(self, instance: object) -> Iterator[_SchemaError]: ...


def _strict_integer(_checker: object, value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)  # 8765.0 is not a port


@functools.lru_cache(maxsize=1)
def _validator() -> _Validator:
    try:  # declared dependency (docs/dependencies.md); lazy, and typed through _Validator (no stubs needed)
        jsonschema = importlib.import_module("jsonschema")
    except ImportError:
        raise DependencyMissing("jsonschema") from None
    try:  # a jsonschema too old for draft 2020-12 is a missing dependency too (exit 2, no traceback)
        base = jsonschema.Draft202012Validator
    except AttributeError:
        raise DependencyMissing("jsonschema") from None
    checker = base.TYPE_CHECKER.redefine("integer", _strict_integer)
    validator: _Validator = jsonschema.validators.extend(base, type_checker=checker)(schema())
    return validator


def _schema_errors(own: dict[Any, Any]) -> dict[str, str]:
    """``path -> message`` for every schema violation, the first one per path."""
    found: dict[str, str] = {}
    for err in _validator().iter_errors(own):
        path = path_str(err.absolute_path)
        if path not in found:
            found[path] = message(path, err.schema, err.instance, str(err.validator))
    return found


def check(extra: Any) -> CheckResult:
    """Validate ``gateway.platforms.glasses.extra``; collect every error, never raise."""
    if extra is None:
        extra = {}
    if not isinstance(extra, Mapping):
        return CheckResult(None, [message("extra", schema(), extra)], [])
    errors = [auth_key_message(key) for key in sorted(HERMES_AUTH_KEYS & set(extra), key=str)]
    warnings: list[str] = []
    own = prune(extra, schema(), "", warnings)
    found = _schema_errors(own)
    found.update(semantic_errors(own, set(found), schema()))
    errors.extend(found.values())
    if errors:
        return CheckResult(None, errors, warnings)
    return CheckResult(build(own), errors, warnings)


def load(extra: Any) -> GlassesConfig:
    """Validated config or ``ConfigError`` listing every problem."""
    result = check(extra)
    if result.errors or result.config is None:
        raise ConfigError(result.errors)
    return result.config


def resolve_state_dir(cfg: GlassesConfig, hermes_home: os.PathLike[str] | str) -> Path:
    """``state_dir`` or ``<hermes_home>/glasses`` (the caller passes the active Hermes home)."""
    return Path(cfg.state_dir) if cfg.state_dir else Path(hermes_home) / PLATFORM_NAME


def environ_reader(environ: Mapping[str, str] | None = None) -> Callable[[str], str]:
    env = os.environ if environ is None else environ
    return lambda name: (env.get(name) or "").strip()


def allow_all_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Any non-blank ``GLASSES_ALLOW_ALL_USERS`` in *environ* (default ``os.environ``)."""
    return bool(environ_reader(environ)(ALLOW_ALL_ENV))


def validate_config(platform_config: Any, *, log_level: int = logging.WARNING) -> bool:
    """Gateway hook: True when the platform may be built. Logs every reason as ``glasses.<path>: …``;
    never raises.

    Also refuses the platform when any profile scope has an allow-all switch or a wildcard
    allowlist (``glasses.gate``); the adapter repeats that check at runtime.
    """
    try:
        from . import gate  # stdlib-only at import; touches Hermes only if Hermes is already loaded

        ok = True
        for problem in gate.violations():
            logger.log(log_level, "glasses config invalid: %s", problem)
            ok = False
        result = check(extra_of(platform_config))
        for warning in result.warnings:
            logger.log(log_level, "glasses config: %s.%s", PLATFORM_NAME, warning)
        for error in result.errors:
            logger.log(log_level, "glasses config invalid: %s.%s", PLATFORM_NAME, error)
        return ok and not result.errors
    except DependencyMissing as exc:  # one clear line naming the module, no traceback
        logger.error("glasses config invalid: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - outermost gateway hook: a validator must not crash the gateway
        logger.log(log_level, "glasses config validation error: %s", type(exc).__name__)
        return False
