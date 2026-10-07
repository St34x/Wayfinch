# SPDX-License-Identifier: Apache-2.0
"""Pure core of the plugin configuration: names, limits, value types and messages.

No I/O and no third-party import (a core module in ``.importlinter``): the schema arrives as a parsed mapping.
``glasses.config`` re-exports the public names and adds the shell (schema file, ``jsonschema``, logging,
``os.environ``); ``glasses.config_access`` holds the access-control rules and ``glasses.config_values`` the value rules
that need stdlib modules outside the core allowlist.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

PLATFORM_NAME = "glasses"

# Lane and platform names: the same rule as protocol/envelope.schema.json and config.schema.json
# $defs (contract tests in test_protocol_bounds.py / test_config_schema.py).
LANE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
PLATFORM_RE = LANE_RE
# Keys the gateway itself puts into PlatformConfig.extra at the pinned Hermes: not ours, never
# warned about (gateway/run_adapters.py:1855 group/thread_sessions_per_user;
# gateway/config_loader.py:161,373 ``_enabled_explicit``).
HERMES_EXTRA_KEYS = frozenset({"group_sessions_per_user", "thread_sessions_per_user", "_enabled_explicit"})
# = protocol welcome.lanes maxItems (protocol/envelope.schema.json; contract test_protocol_bounds.py).
MAX_LANES = 32

# msg.out byte budgets (UTF-8 bytes; protocol/README.md "Limits", checked by test_protocol_bounds.py).
MSG_OUT_TEXT_MAX_BYTES = 2_048
MSG_OUT_TEXT_LONGREF_MAX_BYTES = 512
LONG_FULLTEXT_MAX_BYTES = 4_096
# long_threshold counts characters; at most 4 UTF-8 bytes each, so a head below it always fits msg.out.text.
MAX_LONG_THRESHOLD = MSG_OUT_TEXT_MAX_BYTES // 4  # 512

# Equal to the schema's "default" values (test_config_schema.py).
DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    "bridge_port": 8766,
    "state_dir": None,
    "public_url": "",
    "lanes": ["assistant"],
    "home_lane": "assistant",
    "client_cert_header": "X-Client-Cert",
    "client_cert_encoding": "pem-urlescaped",
    "long_threshold": 280,
    "long_answers": {"platform": "telegram"},
    "edge": {"kind": "nginx", "tls_dir": ""},
    "meeting": {},
}

ECHO_MAX_CHARS = 64
MISSING = object()


@dataclass(frozen=True)
class GlassesConfig:
    host: str
    port: int
    bridge_port: int
    state_dir: str | None
    public_url: str
    lanes: tuple[str, ...]
    home_lane: str
    client_cert_header: str
    client_cert_encoding: str
    long_threshold: int
    long_answers_platform: str
    edge_kind: str
    edge_tls_dir: str
    language_hints: tuple[str, ...] = ()  # meeting.language_hints (reserved; read by config check)

    @property
    def home_chat_id(self) -> str | None:
        return f"{PLATFORM_NAME}:{self.home_lane}" if self.home_lane else None


@dataclass
class CheckResult:
    config: GlassesConfig | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("invalid glasses config: " + "; ".join(self.errors))


class DependencyMissing(RuntimeError):
    """A Python module the plugin needs is not installed; the message names it (no traceback for users)."""

    def __init__(self, module: str):
        super().__init__(
            f"the Python module '{module}' is not installed in this Python — the glasses plugin "
            f"needs it; install it into the Hermes venv (pip install {module})"
        )


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def may_hold_credentials(text: str) -> bool:
    """``user:pass@host`` or ``scheme://`` shape: never shown in a message, a log line or an exception."""
    return "@" in text or "://" in text


def key_name(key: Any) -> str:
    """How an unknown key's name appears in a message: as written, unless it may hold credentials."""
    name = str(key)
    return "<a key containing '@' or '://'>" if may_hold_credentials(name) else name


def describe(value: Any, echo: bool) -> str:
    """How a value appears in a message: scalars as JSON, a string only when *echo*, else its shape."""
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        if not echo or may_hold_credentials(value):
            return f"a string of {_count(len(value), 'character')}"
        shown = json.dumps(value[:ECHO_MAX_CHARS])
        return shown if len(value) <= ECHO_MAX_CHARS else shown + "..."
    if isinstance(value, list):
        return f"a list of {_count(len(value), 'item')}"
    if isinstance(value, Mapping):
        return f"a mapping with {_count(len(value), 'key')}"
    return f"a value of type {type(value).__name__}"


def _repeated(items: Any) -> str:
    """The first repeated item of a list, for ``uniqueItems`` messages."""
    for i, item in enumerate(items):
        if item in items[:i]:
            return f"{describe(item, True)} twice"
    return describe(items, True)


def message(path: str, node: Mapping[str, Any], value: Any, kind: str = "") -> str:
    """``<path>: <problem> (got <value>) — <hint>`` from the schema *node*; *kind* picks an ``x-errors`` text."""
    problem = node.get("x-errors", {}).get(kind) or node.get("x-error", "is not valid")
    echo = bool(node.get("x-echo"))
    got = _repeated(value) if kind == "uniqueItems" and echo else describe(value, echo)
    text = f"{path}: {problem} (got {got})"
    hint = node.get("x-hint")
    return f"{text} — {hint}" if hint else text


def path_str(parts: Iterable[Any]) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def schema_node(schema: Mapping[str, Any], keys: tuple[str, ...]) -> Mapping[str, Any]:
    node = schema
    for key in keys:
        node = node["properties"][key]
    return node


def lookup(own: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    value: Any = own
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            return MISSING
        value = value[key]
    return value


def touched(path: str, failed: Iterable[str]) -> bool:
    """*path*, a parent or a child of it already has a message."""
    return any(f == path or f.startswith((path + ".", path + "[")) or path.startswith(f + ".") for f in failed)


def strip_lane_prefix(value: str) -> str:
    prefix = PLATFORM_NAME + ":"
    return value[len(prefix) :] if value.startswith(prefix) else value


def relation_errors(own: Mapping[str, Any], failed: set[str], schema: Mapping[str, Any]) -> dict[str, str]:
    """``bridge_port`` differs from ``port``; ``home_lane`` is one of ``lanes`` unless ``lanes`` is ``[]``."""
    v = {**DEFAULTS, **own}
    found: dict[str, str] = {}
    if not (touched("port", failed) or touched("bridge_port", failed)) and v["port"] == v["bridge_port"]:
        found["bridge_port"] = message(
            "bridge_port", schema_node(schema, ("bridge_port",)), v["bridge_port"], "x-differ"
        )
    if touched("lanes", failed) or touched("home_lane", failed):
        return found
    home = strip_lane_prefix(v["home_lane"])
    if v["lanes"] and home and home not in v["lanes"]:
        found["home_lane"] = message("home_lane", schema_node(schema, ("home_lane",)), v["home_lane"], "x-in-lanes")
    return found


def is_long(text: str, threshold: int) -> bool:
    """True when *text* goes to the long-answer platform: more than *threshold* characters, or more
    UTF-8 bytes than ``msg.out.text`` holds (byte backstop; lone surrogates count 3 bytes). Shape rules
    (tables, code, images) are the router's (T1.41)."""
    return len(text) > threshold or len(text.encode("utf-8", "surrogatepass")) > MSG_OUT_TEXT_MAX_BYTES


def extra_of(platform_config: Any) -> Any:
    """``extra`` of a Hermes ``PlatformConfig`` (or of a plain dict with an ``extra`` key)."""
    if isinstance(platform_config, Mapping):
        return platform_config.get("extra")
    return getattr(platform_config, "extra", None)
