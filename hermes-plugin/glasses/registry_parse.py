# SPDX-License-Identifier: Apache-2.0
"""Read side of ``devices.json`` (docs/architecture.md section 4): the registry error types, the name/lane/
fingerprint rules shared with ``Registry.add`` and the strict parse of the file content.

Fail closed: bad JSON (also too deep, a repeated key, or NaN in any field), a wrong top level, an entry that breaks the
rules ``add`` enforces (``certFingerprint`` must be a string; ``features`` is empty until a task writes it),
a repeated id or fingerprint all raise ``RegistryCorruptError``. Messages never carry file content.

``MAX_FILE_BYTES`` (4 MiB) is derived from a maximal valid file: an entry with a 64-character astral name
(12 bytes each as ``json.dumps`` escapes it), 32 distinct 32-character lanes and set timestamps takes 2,583
bytes at ``indent=2`` (a typical entry 428), so the cap holds over 1,600 maximal or 9,800 typical entries.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import LANE_RE, MAX_LANES

FILE_NAME = "devices.json"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 4 * 1024 * 1024
NAME_MAX = 64
HEX64_RE = re.compile(r"[0-9a-f]{64}")
ID_RE = re.compile(r"[0-9a-f]{16}")
ENTRY_KEYS = frozenset({
    "deviceId",
    "name",
    "tokenSha256",
    "certFingerprint",
    "lanes",
    "features",
    "createdAt",
    "lastSeen",
    "revokedAt",
})


class RegistryError(Exception):
    """Base of every registry failure; messages never carry a token or a token hash."""


class RegistryCorruptError(RegistryError):
    pass


class InvalidDeviceError(RegistryError, ValueError):
    pass


def norm_fp(fingerprint: Any) -> str | None:
    if type(fingerprint) is not str:  # exact str: a subclass could override lower()
        return None
    fp = fingerprint.lower()
    return fp if HEX64_RE.fullmatch(fp) else None


def check_name(name: Any) -> None:
    if not (isinstance(name, str) and 0 < len(name) <= NAME_MAX and name == name.strip() and name.isprintable()):
        raise InvalidDeviceError(f"name: 1-{NAME_MAX} printable characters without surrounding whitespace")


def check_lanes(lanes: Any) -> list[str]:
    """The config lane rule (``config.LANE_RE``, ``config.MAX_LANES``, no duplicates); never echoes input."""
    if not isinstance(lanes, list):
        raise InvalidDeviceError("lanes: must be a list of lane names")
    if len(lanes) > MAX_LANES:
        raise InvalidDeviceError(f"lanes: at most {MAX_LANES} lanes")
    for i, lane in enumerate(lanes):
        if not (isinstance(lane, str) and LANE_RE.fullmatch(lane)):
            raise InvalidDeviceError(f"lanes[{i}]: must match {LANE_RE.pattern} (whole string)")
        if lane in lanes[:i]:
            raise InvalidDeviceError(f"lanes[{i}]: duplicate lane")
    return list(lanes)


def _follows_add_rules(e: dict[str, Any]) -> bool:
    """Name and lanes on disk obey exactly the rules ``add`` enforces (a hand edit cannot widen them)."""
    try:
        check_name(e["name"])
        check_lanes(e["lanes"])
    except InvalidDeviceError:
        return False
    return True


def _valid_entry(e: Any) -> bool:
    if not isinstance(e, dict) or set(e) != ENTRY_KEYS:
        return False
    revoked = e["revokedAt"]
    token_ok = (
        e["tokenSha256"] is None
        if revoked is not None
        else (isinstance(e["tokenSha256"], str) and HEX64_RE.fullmatch(e["tokenSha256"]) is not None)
    )
    return (
        token_ok
        and isinstance(e["deviceId"], str)
        and ID_RE.fullmatch(e["deviceId"]) is not None
        and isinstance(e["certFingerprint"], str)  # norm_fp(None) is None: a null would equal its own norm
        and norm_fp(e["certFingerprint"]) == e["certFingerprint"]
        and _follows_add_rules(e)
        and e["features"] == []  # what add() writes; the first task that writes features sets the bound
        and isinstance(e["createdAt"], str)
        and all(e[k] is None or isinstance(e[k], str) for k in ("lastSeen", "revokedAt"))
    )


def _no_repeated_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("repeated key")  # a ValueError is what parse() turns into "not valid JSON"
    return dict(pairs)


def parse(raw: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw, object_pairs_hook=_no_repeated_key)
    except (ValueError, RecursionError):  # RecursionError: a deeply nested document
        raise RegistryCorruptError(f"{FILE_NAME}: not valid JSON") from None
    if not (
        isinstance(data, dict)
        and set(data) == {"version", "devices"}
        and type(data["version"]) is int
        and data["version"] == FORMAT_VERSION
    ):  # not True, not 1.0
        raise RegistryCorruptError(f"{FILE_NAME}: expected {{version: {FORMAT_VERSION}, devices: [...]}}")
    entries = data["devices"]
    if not isinstance(entries, list) or not all(_valid_entry(e) for e in entries):
        raise RegistryCorruptError(f"{FILE_NAME}: malformed device entry")
    for key in ("deviceId", "certFingerprint"):
        if len({e[key] for e in entries}) != len(entries):
            raise RegistryCorruptError(f"{FILE_NAME}: duplicate {key}")
    return entries
