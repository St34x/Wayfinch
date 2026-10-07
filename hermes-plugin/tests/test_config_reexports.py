# SPDX-License-Identifier: Apache-2.0
"""glasses.config keeps every name the other plugin modules import from it.

The pure parts live in config_core / config_access / config_values; callers keep importing through glasses.config.
Scans the plugin package source (no import of the scanned modules beyond glasses.config itself).
"""

from __future__ import annotations

import ast
from pathlib import Path

from glasses import config as gcfg

PACKAGE = Path(gcfg.__file__).resolve().parent
OWN = {"config.py", "config_core.py", "config_access.py", "config_values.py"}


def imported_names() -> dict[str, set[str]]:
    """``file -> names`` taken from ``glasses.config`` by every other module of the package."""
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in OWN:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 1
                   and n.module is None for a in n.names if a.name == "config"}  # fmt: skip
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "config":
                found.setdefault(path.name, set()).update(a.name for a in node.names)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                found.setdefault(path.name, set()).add(node.attr)
    return found


def test_the_scan_finds_the_known_importers() -> None:
    files = set(imported_names())
    assert {"__init__.py", "adapter.py", "admin.py", "gate.py", "hermes_facts.py", "registry.py"} <= files, files


def test_every_name_other_modules_import_is_still_exported() -> None:
    missing = {f: sorted(n for n in names if not hasattr(gcfg, n)) for f, names in imported_names().items()}
    assert {f: n for f, n in missing.items() if n} == {}
