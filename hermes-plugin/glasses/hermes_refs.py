# SPDX-License-Identifier: Apache-2.0
"""Which fields ``glasses-admin config check --live`` does not evaluate because ``config.yaml`` writes an environment
reference (``${VAR}`` / ``${env:VAR}``) in them (T1.196a).

Hermes expands such a reference with the value of an environment variable. A check that then compares the expanded
text (a tool set with the ``telegram`` one, a route ``chat_id`` with a lane, a timeout with zero, a profile name with
the served ones) answers "does the secret equal what I wrote next to it" to whoever can edit ``config.yaml`` and
read the output. So the check never evaluates such a field: it decides from the raw text of ``config.yaml`` alone
(``has_env_ref``: the text holds ``${``, a superset of what Hermes expands; the value of the variable is never
looked at), prints one fixed line (``not_checked``) and skips every check that depends on the field. Stdout, stderr
and the exit status are then the same for every value of the variable.

Tests: tests/test_config_check_live_output_independent_of_env.py, tests/test_config_check_env_references.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")
ANY = "*"  # a path element that matches every value of a mapping

# Profile fields (the keys of one profile's ``config.yaml``, defaults merged), each with every raw key the check
# reads for it. ``TOOLSETS`` holds the keys that decide what ``_get_platform_tools`` returns.
TOOLSETS = "platform_toolsets"
STT_PROVIDER = "stt.provider"
STT_LANGUAGE = "stt.language"
CLARIFY = "agent.clarify_timeout"
APPROVALS = "approvals.timeout"
PROFILE_FIELDS: Mapping[str, tuple[tuple[str, ...], ...]] = {
    TOOLSETS: (
        ("platform_toolsets",),
        ("known_plugin_toolsets",),
        ("known_builtin_toolsets",),
        ("toolsets",),
        ("context", "engine"),
        ("agent", "disabled_toolsets"),
        ("mcp_servers", ANY, "enabled"),
    ),
    STT_PROVIDER: (("stt", "provider"),),
    STT_LANGUAGE: (("stt", "provider"), ("stt", "language"), ("stt", ANY, "language"), ("stt", ANY, "language_code")),
    CLARIFY: (("clarify", "timeout"), ("agent", "clarify_timeout")),
    APPROVALS: (("approvals", "timeout"),),
}

# Gateway fields (the launch home's ``config.yaml``, top level or under ``gateway:``).
ROUTES = "gateway.profile_routes"
MULTIPLEX = "gateway.multiplex_profiles"
ALLOW_ALL = "allow_all_users"
ROUTING_FIELDS = frozenset({ROUTES, MULTIPLEX})
GATEWAY_FIELDS: Mapping[str, tuple[tuple[str, ...], ...]] = {
    ROUTES: (("profile_routes",), ("gateway", "profile_routes")),
    MULTIPLEX: (("multiplex_profiles",), ("gateway", "multiplex_profiles")),
    ALLOW_ALL: (("allow_all_users",), ("gateway", "allow_all_users")),
}
# A security gate that cannot be evaluated is not a pass: it is an error line of the gate's known shape.
ALLOW_ALL_PROBLEM = (
    "active scope: allow_all_users in config.yaml uses an environment reference, not checked - write true or "
    "false there, a grant to every sender cannot be ruled out otherwise"
)
NOT_CHECKED_PLACEHOLDER = "<not checked>"  # stt_provider of a profile whose stt.provider is a reference


def not_checked(field: str) -> str:
    """The one fixed line for *field* (no value in it, whatever the variable holds)."""
    return f"{field}: uses an environment reference, not checked"


def has_env_ref(value: Any) -> bool:
    """Whether a string value anywhere inside *value* holds ``${`` (keys are never expanded by Hermes)."""
    if isinstance(value, str):
        return "${" in value
    if isinstance(value, Mapping):
        return any(has_env_ref(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_env_ref(v) for v in value)
    return False


def _values_at(config: Any, path: tuple[str, ...]) -> Iterable[Any]:
    if not path or isinstance(config, str):  # a string where the check expects a mapping is a value to scan
        yield config
    elif isinstance(config, Mapping):
        children = config.values() if path[0] == ANY else [config[path[0]]] if path[0] in config else []
        for child in children:
            yield from _values_at(child, path[1:])


def _referenced(config: Any, fields: Mapping[str, tuple[tuple[str, ...], ...]]) -> frozenset[str]:
    return frozenset(
        field for field, paths in fields.items() if any(has_env_ref(v) for p in paths for v in _values_at(config, p))
    )


def profile_unchecked(raw_config: Any) -> frozenset[str]:
    """The profile fields *raw_config* (the merged, canonical, NOT expanded config of one profile) writes as refs.
    A stt.provider reference also hides the language: it decides which ``stt.<provider>.language`` is read."""
    return _referenced(raw_config, PROFILE_FIELDS)


def gateway_unchecked(raw_yaml: Any) -> frozenset[str]:
    """The gateway fields the raw ``config.yaml`` mapping writes as a reference."""
    return _referenced(raw_yaml, GATEWAY_FIELDS)


def read_raw_yaml(home: Path) -> Mapping[str, Any]:
    """``<home>/config.yaml`` parsed, nothing expanded; ``{}`` when absent or not a mapping."""
    import yaml

    path = Path(home) / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) if path.exists() else None
    return data if isinstance(data, dict) else {}


def unless_unchecked(field: str, unchecked: Iterable[str], neutral: T, compute: Callable[[], T]) -> T:
    """*compute()*, or *neutral* (the value that raises no problem) without calling it when *field* is unchecked."""
    return neutral if field in unchecked else compute()
