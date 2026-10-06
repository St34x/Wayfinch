# SPDX-License-Identifier: Apache-2.0
"""Configuration of the `glasses` platform plugin.

The settings live in the Hermes config under ``gateway.platforms.glasses.extra``. Keys, types, bounds,
defaults and the user-facing messages are defined once in ``config.schema.json`` (JSON Schema 2020-12,
ENGINEERING.md section 4). ``check()`` validates with it (``jsonschema``, imported lazily; a declared plugin dependency,
see docs/dependencies.md -- Hermes itself ships it only with its ``mcp`` extra; when it is missing ``check()`` raises
``DependencyMissing`` naming the module: one log line in the gateway, exit 2 in ``config check``) and
adds the rules a schema cannot express:

* ``host`` is a loopback IP literal (hostnames, ``localhost`` included, are refused);
* ``public_url`` is empty or a strict ``wss://`` URL, stored in normal form;
* ``state_dir`` and ``edge.tls_dir`` are absolute paths without surrounding whitespace or control
  characters (or empty);
* ``bridge_port`` differs from ``port``; ``home_lane`` is one of ``lanes`` unless ``lanes`` is ``[]``.

Every problem is reported, one message per path: ``<path>: <problem> (got <value>) — <hint>``; callers
print it as ``glasses.<message>``. A string value is shown only for keys the schema marks ``x-echo``
(names, enum values, paths) and never when it contains ``@`` or ``://``; never for URL- or
credential-capable keys (``public_url``, ``meeting.endpoint``/``provider``/``llm.*``) or for unknown or
refused keys. Patterns are anchored and guarded against a trailing newline (Python ``$``); code uses
``re.fullmatch`` only.

Unknown keys are reported as warnings, not errors -- except the access-control keys the Hermes
gateway itself acts on for any platform (``HERMES_AUTH_KEYS``): those are refused, because the
glasses platform admits only enrolled devices listed in ``GLASSES_ALLOWED_USERS``. Keys the gateway
injects (``HERMES_EXTRA_KEYS``) are skipped silently. No Hermes import: unit-testable and reused by
the admin CLI (``glasses.admin config check``).
"""

from __future__ import annotations

import difflib
import functools
import ipaddress
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

PLATFORM_NAME = "glasses"
ALLOWED_USERS_ENV = "GLASSES_ALLOWED_USERS"
# NOT registered with the gateway (registering it is what makes Hermes honour it, see
# gateway/authz_mixin.py:645-651 at the pinned commit). It must never be set; if it is set in any
# profile's environment the platform is refused (validate_config) and the adapter refuses
# connections and inbound events at runtime (glasses.gate).
ALLOW_ALL_ENV = "GLASSES_ALLOW_ALL_USERS"
# Global gateway gates that also admit glasses senders (gateway/authz_mixin.py:666-695).
GATEWAY_ALLOWED_USERS_ENV = "GATEWAY_ALLOWED_USERS"
GATEWAY_ALLOW_ALL_ENV = "GATEWAY_ALLOW_ALL_USERS"
# Hermes' truthy set for allow-all switches (gateway/authz_mixin.py:28 ``_TRUTHY``).
HERMES_TRUTHY = frozenset({"true", "1", "yes"})

# Lane and platform names: the same rule as protocol/envelope.schema.json and config.schema.json
# $defs (contract tests in test_protocol_bounds.py / test_config_schema.py).
LANE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
PLATFORM_RE = LANE_RE
# One DNS label (public_url host); the whole name is at most 253 characters.
DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
PUBLIC_URL_SCHEMES = ("wss",)
# Keys the gateway itself puts into PlatformConfig.extra at the pinned Hermes: not ours, never
# warned about (gateway/run_adapters.py:1855 group/thread_sessions_per_user;
# gateway/config_loader.py:161,373 ``_enabled_explicit``).
HERMES_EXTRA_KEYS = frozenset({"group_sessions_per_user", "thread_sessions_per_user", "_enabled_explicit"})
# Access-control keys the Hermes gateway reads from ANY platform's ``extra`` (pinned a4648c5f58):
# allow_from / group_allow_from / allowed_users -> allowlists where "*" admits everyone
# (gateway/authz_mixin.py:482-507, :81-82); group_allowed_chats (:549-553); dm_policy /
# group_policy (:418-423, :725-731); groups (:425-449); allow_bots (:557-566);
# unauthorized_dm_behavior (:713-717, gateway/config.py:826-833); allow_admin_from /
# user_allowed_commands and the group_* twins (slash-command access, gateway/slash_access.py:1-25,
# gateway/run_inbound.py:878); all bridged into ``extra`` by gateway/config_loader.py:208-221.
# ``allowed_chats``/``allow_all_users`` are refused too so nobody believes they work here.
HERMES_AUTH_KEYS = frozenset({
    "allow_from",
    "group_allow_from",
    "allowed_users",
    "allowed_chats",
    "group_allowed_chats",
    "dm_policy",
    "group_policy",
    "groups",
    "allow_bots",
    "unauthorized_dm_behavior",
    "allow_admin_from",
    "group_allow_admin_from",
    "user_allowed_commands",
    "group_user_allowed_commands",
    "allow_all_users",
})
# = protocol welcome.lanes maxItems (protocol/envelope.schema.json; contract test_protocol_bounds.py).
MAX_LANES = 32

# msg.out byte budgets (UTF-8 bytes; protocol/README.md "Limits", checked by test_protocol_bounds.py).
MSG_OUT_TEXT_MAX_BYTES = 2_048
MSG_OUT_TEXT_LONGREF_MAX_BYTES = 512
LONG_FULLTEXT_MAX_BYTES = 4_096
# long_threshold counts characters; at most 4 UTF-8 bytes each, so a head below it always fits msg.out.text.
MAX_LONG_THRESHOLD = MSG_OUT_TEXT_MAX_BYTES // 4  # 512

# Gateway-side message cap. The long-answer router (docs/architecture.md section 2.2) must see every reply whole,
# so the gateway must not split it first. At the pinned Hermes the gateway splits by
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
# long-answer router (T1.41) cuts every reply to those budgets; replies above this cap arrive in parts.
# The value is the 64 KiB frame cap minus 1 KiB envelope headroom over 6 bytes per character (the longest
# JSON escape, ``\u00XX``). One constant serves the registry field and the class attribute.
FRAME_MAX_BYTES = 65_536
ENVELOPE_HEADROOM_BYTES = 1_024
JSON_MAX_BYTES_PER_CHAR = 6
MAX_MESSAGE_LENGTH = (FRAME_MAX_BYTES - ENVELOPE_HEADROOM_BYTES) // JSON_MAX_BYTES_PER_CHAR  # 10 752

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

SCHEMA_PATH = Path(__file__).with_name("config.schema.json")
ECHO_MAX_CHARS = 64
_MISSING = object()


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


def is_loopback_host(host: Any) -> bool:
    """True only for a loopback IP literal (127.0.0.0/8, ::1).

    Hostnames are refused, ``localhost`` included: what they resolve to is outside the plugin's
    control, and binding beyond loopback would expose the gateway (docs/architecture.md section 4).
    """
    if not isinstance(host, str) or host != host.strip():
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and (addr.ipv4_mapped is not None or addr.scope_id):
        return False  # only plain ::1; Python >= 3.13 calls ::ffff:127.0.0.1 loopback too
    return addr.is_loopback


def _is_clean_str(value: Any) -> bool:
    """A string without leading/trailing whitespace and without control/format characters."""
    return isinstance(value, str) and value == value.strip() and value.isprintable()


def _empty_or_abs_path(value: Any) -> bool:
    return value in (None, "") or (_is_clean_str(value) and os.path.isabs(value))


def _public_host(hostname: str) -> str | None:
    """Normalised host of ``public_url`` (lower-case DNS name or IP literal), or None if invalid."""
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        addr = None
    if addr is not None:
        if addr.is_unspecified or (isinstance(addr, ipaddress.IPv6Address) and addr.scope_id):
            return None
        return f"[{addr.compressed}]" if addr.version == 6 else addr.compressed
    name = hostname.lower()
    if len(name) > 253 or not all(DNS_LABEL_RE.fullmatch(label) for label in name.split(".")):
        return None  # also refuses a trailing dot, empty labels and non-ASCII (use punycode)
    if name.replace(".", "").isdigit():
        return None  # all-numeric names are IPv4 shorthands (127.1), not DNS names
    return name


def normalise_public_url(value: Any) -> str | None:
    """``public_url`` in normal form (``wss://host[:port]/path``), ``""`` for unset, None if invalid.

    Strict: printable ASCII only, no whitespace anywhere (``urlsplit`` silently strips tab/CR/LF and
    surrounding spaces, but the raw value would go into the enrolment QR), scheme ``wss`` only, a
    host (DNS name or IP literal, not unspecified), no credentials, explicit port 1-65535, no query or
    fragment (a bare ``?``/``#`` is dropped). The default port 443 is removed, the host lower-cased.
    """
    if value == "":
        return ""
    if not isinstance(value, str) or not value or not all(0x21 <= ord(c) <= 0x7E for c in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        return None
    if parts.scheme not in PUBLIC_URL_SCHEMES or not value.startswith(parts.scheme + "://"):
        return None
    if "@" in parts.netloc or parts.query or parts.fragment or parts.netloc.endswith(":"):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    host = _public_host(parts.hostname or "")
    if host is None:
        return None
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))


def _valid_public_url(value: Any) -> bool:
    return normalise_public_url(value) is not None


# --------------------------------------------------------------------------------------------
# schema-driven validation and messages
# --------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    """The parsed ``config.schema.json`` (read once)."""
    loaded: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return loaded


def _strict_integer(_checker: Any, value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)  # 8765.0 is not a port


class DependencyMissing(RuntimeError):
    """A Python module the plugin needs is not installed; the message names it (no traceback for users)."""

    def __init__(self, module: str):
        super().__init__(
            f"the Python module '{module}' is not installed in this Python — the glasses plugin "
            f"needs it; install it into the Hermes venv (pip install {module})"
        )


@functools.lru_cache(maxsize=1)
def _validator() -> Any:
    try:
        from jsonschema import Draft202012Validator, validators  # declared dependency (docs/dependencies.md); lazy
    except ImportError:
        raise DependencyMissing("jsonschema") from None

    checker = Draft202012Validator.TYPE_CHECKER.redefine("integer", _strict_integer)
    return validators.extend(Draft202012Validator, type_checker=checker)(schema())


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def may_hold_credentials(text: str) -> bool:
    """``user:pass@host`` or ``scheme://`` shape: never shown in a message, a log line or an exception."""
    return "@" in text or "://" in text


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


def _path_str(parts: Iterable[Any]) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def _node(keys: tuple[str, ...]) -> Mapping[str, Any]:
    node: Mapping[str, Any] = schema()
    for key in keys:
        node = node["properties"][key]
    return node


def _lookup(own: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    value: Any = own
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _touched(path: str, failed: Iterable[str]) -> bool:
    """*path*, a parent or a child of it already has a message."""
    return any(f == path or f.startswith((path + ".", path + "[")) or path.startswith(f + ".") for f in failed)


def _unknown(prefix: str, key: Any, known: Iterable[str]) -> str:
    text = f"{prefix}{key}: unknown key, ignored"
    close = difflib.get_close_matches(key, list(known), n=1) if isinstance(key, str) else []
    return f"{text} — did you mean {prefix}{close[0]}?" if close else text


def _prune(raw: Mapping[Any, Any], node: Mapping[str, Any], prefix: str, warnings: list[str]) -> dict[Any, Any]:
    """Copy of *raw* with only the keys the schema *node* declares (recursively); the rest -> warnings."""
    props = node.get("properties", {})
    out: dict[Any, Any] = {}
    for key, value in raw.items():
        if key not in props:
            if prefix or key not in HERMES_EXTRA_KEYS | HERMES_AUTH_KEYS:
                warnings.append(_unknown(prefix, key, props))
        elif isinstance(value, Mapping) and "properties" in props[key]:
            out[key] = _prune(value, props[key], f"{prefix}{key}.", warnings)
        else:
            out[key] = value
    return out


def _schema_errors(own: dict[Any, Any]) -> dict[str, str]:
    """``path -> message`` for every schema violation, the first one per path."""
    found: dict[str, str] = {}
    for err in _validator().iter_errors(own):
        path = _path_str(err.absolute_path)
        if path not in found:
            found[path] = message(path, err.schema, err.instance, str(err.validator))
    return found


# Rules a schema cannot express, checked only where the schema found nothing.
SEMANTIC_RULES: tuple[tuple[tuple[str, ...], Callable[[Any], bool]], ...] = (
    (("host",), is_loopback_host),
    (("public_url",), _valid_public_url),
    (("state_dir",), _empty_or_abs_path),
    (("edge", "tls_dir"), _empty_or_abs_path),
)


def _strip_lane_prefix(value: str) -> str:
    prefix = PLATFORM_NAME + ":"
    return value[len(prefix) :] if value.startswith(prefix) else value


def _relation_errors(own: Mapping[str, Any], failed: set[str]) -> dict[str, str]:
    v = {**DEFAULTS, **own}
    found: dict[str, str] = {}
    if not (_touched("port", failed) or _touched("bridge_port", failed)) and v["port"] == v["bridge_port"]:
        found["bridge_port"] = message("bridge_port", _node(("bridge_port",)), v["bridge_port"], "x-differ")
    if _touched("lanes", failed) or _touched("home_lane", failed):
        return found
    home = _strip_lane_prefix(v["home_lane"])
    if v["lanes"] and home and home not in v["lanes"]:
        found["home_lane"] = message("home_lane", _node(("home_lane",)), v["home_lane"], "x-in-lanes")
    return found


def _semantic_errors(own: Mapping[str, Any], failed: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for keys, ok in SEMANTIC_RULES:
        path = ".".join(keys)
        value = _lookup(own, keys)
        if value is not _MISSING and not _touched(path, failed) and not ok(value):
            found[path] = message(path, _node(keys), value)
    found.update(_relation_errors(own, failed | set(found)))
    return found


def _auth_key_message(key: str) -> str:
    return (
        f"{key}: not allowed for the glasses platform (got a value, not shown) — the Hermes gateway "
        f"would act on it (an allowlist with '*' or an open policy admits everyone); the platform "
        f"admits only enrolled devices listed in {ALLOWED_USERS_ENV}"
    )


def _build(own: Mapping[str, Any]) -> GlassesConfig:
    v = {**DEFAULTS, **own}
    long_answers = {**DEFAULTS["long_answers"], **(v["long_answers"] or {})}
    edge = {**DEFAULTS["edge"], **(v["edge"] or {})}
    meeting = v["meeting"] or {}
    return GlassesConfig(
        host=v["host"],
        port=v["port"],
        bridge_port=v["bridge_port"],
        state_dir=v["state_dir"] or None,
        public_url=normalise_public_url(v["public_url"]) or "",
        lanes=tuple(v["lanes"]),
        home_lane=_strip_lane_prefix(v["home_lane"]),
        client_cert_header=v["client_cert_header"],
        client_cert_encoding=v["client_cert_encoding"],
        long_threshold=v["long_threshold"],
        long_answers_platform=long_answers["platform"],
        edge_kind=edge["kind"],
        edge_tls_dir=edge["tls_dir"] or "",
        language_hints=tuple(meeting.get("language_hints") or ()),
    )


def check(extra: Any) -> CheckResult:
    """Validate ``gateway.platforms.glasses.extra``; collect every error, never raise."""
    if extra is None:
        extra = {}
    if not isinstance(extra, Mapping):
        return CheckResult(None, [message("extra", schema(), extra)], [])
    errors = [_auth_key_message(key) for key in sorted(HERMES_AUTH_KEYS & set(extra), key=str)]
    warnings: list[str] = []
    own = _prune(extra, schema(), "", warnings)
    found = _schema_errors(own)
    found.update(_semantic_errors(own, set(found)))
    errors.extend(found.values())
    if errors:
        return CheckResult(None, errors, warnings)
    return CheckResult(_build(own), errors, warnings)


def is_long(text: str, threshold: int) -> bool:
    """True when *text* goes to the long-answer platform: more than *threshold* characters, or more
    UTF-8 bytes than ``msg.out.text`` holds (byte backstop; lone surrogates count 3 bytes). Shape rules
    (tables, code, images) are the router's (T1.41)."""
    return len(text) > threshold or len(text.encode("utf-8", "surrogatepass")) > MSG_OUT_TEXT_MAX_BYTES


def load(extra: Any) -> GlassesConfig:
    """Validated config or ``ConfigError`` listing every problem."""
    result = check(extra)
    if result.errors:
        raise ConfigError(result.errors)
    assert result.config is not None
    return result.config


def resolve_state_dir(cfg: GlassesConfig, hermes_home: os.PathLike | str) -> Path:
    """``state_dir`` or ``<hermes_home>/glasses`` (the caller passes the active Hermes home)."""
    return Path(cfg.state_dir) if cfg.state_dir else Path(hermes_home) / PLATFORM_NAME


def extra_of(platform_config: Any) -> Any:
    """``extra`` of a Hermes ``PlatformConfig`` (or of a plain dict with an ``extra`` key)."""
    if isinstance(platform_config, Mapping):
        return platform_config.get("extra")
    return getattr(platform_config, "extra", None)


# --------------------------------------------------------------------------------------------
# allow-all / wildcard gate (pure; glasses.gate supplies the per-profile readers)
# --------------------------------------------------------------------------------------------


def parse_allowlist(raw: Any) -> set[str]:
    """Entries of an env allowlist, parsed like Hermes does: a JSON list literal or a comma-separated
    string, entries stripped, blanks dropped (gateway/authz_mixin.py:71-78 ``_coerce_allow_set`` +
    gateway/platforms/_shared.py:89-103 ``decode_json_list_literal``)."""
    if raw is None:
        return set()
    if isinstance(raw, str) and raw.lstrip()[:1] == "[":
        try:
            loaded = json.loads(raw)
        except ValueError:
            loaded = raw
        if isinstance(loaded, list):
            return {str(part).strip() for part in loaded if str(part).strip()}
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def gate_violations(read: Callable[[str], str]) -> list[str]:
    """Why the gateway would admit glasses senders without a device allowlist; ``[]`` = fine.

    ``read(name)`` returns an env value ("" when unset) as the gateway sees it in one scope. Messages
    name the variable, never its value.
    """
    problems: list[str] = []
    if (read(ALLOW_ALL_ENV) or "").strip():
        problems.append(
            f"{ALLOW_ALL_ENV} is set; it must never be set (list device ids in {ALLOWED_USERS_ENV} instead)"
        )
    own = parse_allowlist(read(ALLOWED_USERS_ENV))
    gateway_list = parse_allowlist(read(GATEWAY_ALLOWED_USERS_ENV))
    for name, entries in ((ALLOWED_USERS_ENV, own), (GATEWAY_ALLOWED_USERS_ENV, gateway_list)):
        if any("*" in entry for entry in entries):
            problems.append(f"{name} contains a wildcard '*'; list device ids explicitly")
    if (read(GATEWAY_ALLOW_ALL_ENV) or "").strip().lower() in HERMES_TRUTHY and not (own or gateway_list):
        problems.append(
            f"{GATEWAY_ALLOW_ALL_ENV} is enabled and {ALLOWED_USERS_ENV} is empty: the gateway "
            f"would admit every glasses sender; set {ALLOWED_USERS_ENV}"
        )
    return problems


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
    except Exception as exc:  # defensive: a validator must not crash the gateway
        logger.warning("glasses config validation error: %s", type(exc).__name__)
        return False
