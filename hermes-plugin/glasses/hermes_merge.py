# SPDX-License-Identifier: Apache-2.0
"""The glasses platform block of a Hermes config as the pinned gateway merges it (pure, no Hermes import).

``config check`` must validate what the gateway validates. At the pinned Hermes (a4648c5f58) the gateway
does not read only ``gateway.platforms.glasses.extra``:

* ``gateway/config_loader.py:139-171`` ``merge_platform_sections``: the blocks ``gateway.platforms.glasses``
  -> ``platforms.glasses`` -> ``gateway.glasses`` are merged in that order (later wins per key; their
  ``extra`` mappings are merged the same way);
* ``gateway/config.py:446-453`` ``PlatformConfig.from_dict``: every key of the merged block that is not a
  typed ``PlatformConfig`` field (``_TYPED_KEYS``) is promoted into ``extra``; an explicit ``extra`` value
  wins on a clash -- so ``gateway.platforms.glasses.host`` or ``.allow_from`` count like ``extra.host``;
* ``gateway/config_loader.py:182-191, 226-245, 333-374``: a top-level ``glasses:`` block wins as the
  platform section; its non-typed keys and its own ``extra`` are bridged into ``extra`` (authored
  ``extra`` from the merged blocks wins), and its ``enabled`` is used.

``gateway.glasses`` counts only when Hermes knows the plugin platform at merge time; it is always included
here, the conservative side (a check may refuse what the gateway ignores, never the reverse). Not covered
statically: the legacy ``gateway.json`` and a managed overlay (``--live`` reads Hermes itself).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config_core import PLATFORM_NAME

# PlatformConfig._TYPED_KEYS at the pin (gateway/config.py:440-443); every other block key goes to extra.
HERMES_TYPED_KEYS = frozenset({
    "enabled",
    "token",
    "api_key",
    "home_channel",
    "reply_to_mode",
    "channel_overrides",
    "extra",
    "gateway_restart_notification",
    "typing_indicator",
    "typing_status_text",
})
LOCATIONS = ("gateway.platforms.glasses", "platforms.glasses", "gateway.glasses", "glasses")


@dataclass(frozen=True)
class PlatformView:
    enabled: Any  # as written (the gateway coerces bool-ish strings)
    extra: Any  # the merged extra mapping, or a non-mapping ``extra`` as written (check() reports it)


def _get(data: Any, key: str) -> Any:
    return data.get(key) if isinstance(data, Mapping) else None


def _promoted(block: Mapping[Any, Any]) -> dict[Any, Any]:
    return {k: v for k, v in block.items() if k not in HERMES_TYPED_KEYS}


def _nested_blocks(data: Any) -> list[Mapping[Any, Any]]:
    gateway = _get(data, "gateway")
    found = (
        _get(_get(gateway, "platforms"), PLATFORM_NAME),
        _get(_get(data, "platforms"), PLATFORM_NAME),
        _get(gateway, PLATFORM_NAME),
    )
    return [block for block in found if isinstance(block, Mapping)]


def platform_view(data: Any) -> PlatformView | None:
    """``enabled`` and the effective ``extra`` of the glasses platform; None when no block exists."""
    blocks = _nested_blocks(data)
    root = _get(data, PLATFORM_NAME)
    root = root if isinstance(root, Mapping) else None
    if not blocks and root is None:
        return None
    merged: dict[Any, Any] = {}
    authored: dict[Any, Any] = {}
    for block in blocks:
        raw = block.get("extra")
        if raw is not None and not isinstance(raw, Mapping):
            return PlatformView(block.get("enabled"), raw)
        merged.update(block)
        authored.update(raw or {})
    extra = {**_promoted(merged), **authored}
    enabled = merged.get("enabled")
    if root is not None:
        root_extra = root.get("extra")
        bridged = {**_promoted(root), **(root_extra if isinstance(root_extra, Mapping) else {})}
        extra.update({k: authored.get(k, v) for k, v in bridged.items()})
        enabled = root.get("enabled", enabled)
    return PlatformView(enabled, extra)
