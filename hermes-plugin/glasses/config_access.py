# SPDX-License-Identifier: Apache-2.0
"""Access-control rules of the plugin configuration (pure core): who the gateway would admit as a glasses sender.

The env switches and allowlists the gateway honours for glasses senders, the ``extra`` keys the gateway acts on for
any platform, and the messages for both. No I/O (a core module in ``.importlinter``): the environment arrives as a
``read(name)`` callable; ``glasses.gate`` and ``glasses.config`` supply the readers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

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


def auth_key_message(key: str) -> str:
    return (
        f"{key}: not allowed for the glasses platform (got a value, not shown) — the Hermes gateway "
        f"would act on it (an allowlist with '*' or an open policy admits everyone); the platform "
        f"admits only enrolled devices listed in {ALLOWED_USERS_ENV}"
    )


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
