# SPDX-License-Identifier: Apache-2.0
"""Value rules of the plugin configuration that a schema cannot express, and building the config value.

Pure (no I/O), but not a core module: it needs ``ipaddress``, ``difflib`` and ``os.path``, which the core import
allowlist (``tools/quality/check_core_purity.py``) does not list. The schema arrives as a parsed mapping.

* ``host`` is a loopback IP literal (hostnames, ``localhost`` included, are refused);
* ``public_url`` is empty or a strict ``wss://`` URL, stored in normal form;
* ``state_dir`` and ``edge.tls_dir`` are absolute paths without surrounding whitespace or control
  characters (or empty);
* keys the schema does not declare are dropped with a warning (``prune``).
"""

from __future__ import annotations

import difflib
import ipaddress
import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config_access import HERMES_AUTH_KEYS
from .config_core import (
    DEFAULTS,
    HERMES_EXTRA_KEYS,
    MISSING,
    GlassesConfig,
    key_name,
    lookup,
    message,
    relation_errors,
    schema_node,
    strip_lane_prefix,
    touched,
)

# One DNS label (public_url host); the whole name is at most 253 characters.
DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
PUBLIC_URL_SCHEMES = ("wss",)


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


# Rules a schema cannot express, checked only where the schema found nothing.
SEMANTIC_RULES: tuple[tuple[tuple[str, ...], Callable[[Any], bool]], ...] = (
    (("host",), is_loopback_host),
    (("public_url",), _valid_public_url),
    (("state_dir",), _empty_or_abs_path),
    (("edge", "tls_dir"), _empty_or_abs_path),
)


def semantic_errors(own: Mapping[str, Any], failed: set[str], schema: Mapping[str, Any]) -> dict[str, str]:
    """``path -> message`` for the rules above and the relations, where the schema found nothing."""
    found: dict[str, str] = {}
    for keys, ok in SEMANTIC_RULES:
        path = ".".join(keys)
        value = lookup(own, keys)
        if value is not MISSING and not touched(path, failed) and not ok(value):
            found[path] = message(path, schema_node(schema, keys), value)
    found.update(relation_errors(own, failed | set(found), schema))
    return found


def _unknown(prefix: str, key: Any, known: Iterable[str]) -> str:
    text = f"{prefix}{key_name(key)}: unknown key, ignored"
    close = difflib.get_close_matches(key, list(known), n=1) if isinstance(key, str) else []
    return f"{text} — did you mean {prefix}{close[0]}?" if close else text


def prune(raw: Mapping[Any, Any], node: Mapping[str, Any], prefix: str, warnings: list[str]) -> dict[Any, Any]:
    """Copy of *raw* with only the keys the schema *node* declares (recursively); the rest -> warnings."""
    props = node.get("properties", {})
    out: dict[Any, Any] = {}
    for key, value in raw.items():
        if key not in props:
            if prefix or key not in HERMES_EXTRA_KEYS | HERMES_AUTH_KEYS:
                warnings.append(_unknown(prefix, key, props))
        elif isinstance(value, Mapping) and "properties" in props[key]:
            out[key] = prune(value, props[key], f"{prefix}{key}.", warnings)
        else:
            out[key] = value
    return out


def build(own: Mapping[str, Any]) -> GlassesConfig:
    """The config value of a valid *own* mapping (pruned, no errors), defaults filled in."""
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
        home_lane=strip_lane_prefix(v["home_lane"]),
        client_cert_header=v["client_cert_header"],
        client_cert_encoding=v["client_cert_encoding"],
        long_threshold=v["long_threshold"],
        long_answers_platform=long_answers["platform"],
        edge_kind=edge["kind"],
        edge_tls_dir=edge["tls_dir"] or "",
        language_hints=tuple(meeting.get("language_hints") or ()),
    )
