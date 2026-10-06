# SPDX-License-Identifier: Apache-2.0
"""Scratch-copy helpers of the import-boundary gate tests (T1.100; test_imports*.py).

Every test plants its own state into a scratch copy of `hermes-plugin/glasses` plus the real `.importlinter`: its own
core module `glasses._planted_core` (added to the core list of the copy), its own helper modules and its own ratchet
entries under a `# T9.99` line. So the tests do not depend on today's core list or ratchet entries: a task that adds a
core module or removes a ratchet entry from the real files does not turn them red.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TOP = Path(__file__).resolve().parents[3]
LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"
SECTION = "[importlinter:contract:core-purity]"
CORE = "glasses._planted_core"
CORE_FILE = Path("hermes-plugin/glasses/_planted_core.py")
TEST_ID = "# T9.99 test-only ratchet entry"


def _key_of(line: str) -> str | None:
    if line[:1].isspace() or "=" not in line:
        return None
    return line.split("=", 1)[0].strip()


def _section(lines: list[str]) -> tuple[int, int]:
    start = next(i for i, line in enumerate(lines) if line.strip() == SECTION)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    return start, end


def _block_end(lines: list[str], key_at: int, end: int) -> int:
    i = key_at + 1
    while i < end and lines[i].strip() and (lines[i][:1].isspace() or lines[i].lstrip().startswith("#")):
        i += 1
    return i


def _edit_values(config: Path, key: str, values: list[str], replace: bool) -> None:
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = _section(lines)
    key_at = next((i for i in range(start + 1, end) if _key_of(lines[i]) == key), None)
    new = [f"    {value}\n" for value in values]
    if key_at is None:
        last = max(i for i in range(start, end) if lines[i].strip())
        lines[last + 1 : last + 1] = [f"{key} =\n", *new]
    elif replace:
        lines[key_at : _block_end(lines, key_at, end)] = [f"{key} =\n", *new]
    else:
        lines[key_at + 1 : key_at + 1] = new
    config.write_text("".join(lines), encoding="utf-8")


def add_values(config: Path, key: str, values: list[str]) -> None:
    """Prepend list values (or comment lines) to `key` of the core-purity contract; create the key if missing."""
    _edit_values(config, key, values, replace=False)


def set_values(config: Path, key: str, values: list[str]) -> None:
    """Replace the whole list of `key` of the core-purity contract."""
    _edit_values(config, key, values, replace=True)


def add_ignore(config: Path, entry: str, comment: str | None = TEST_ID) -> None:
    """A ratchet entry of the core-purity contract, under its own task-id comment line unless `comment` is None."""
    add_values(config, "ignore_imports", ([comment] if comment else []) + [entry])


def make_tree(root: Path) -> Path:
    """Scratch copy of the plugin package and the real contract, with the planted core module in the core list."""
    skip = shutil.ignore_patterns("__pycache__")
    shutil.copytree(TOP / "hermes-plugin/glasses", root / "hermes-plugin/glasses", ignore=skip)
    shutil.copy(TOP / ".importlinter", root / ".importlinter")
    (root / CORE_FILE).write_text("from __future__ import annotations\n\nX = 1\n", encoding="utf-8")
    add_values(root / ".importlinter", "source_modules", [CORE])
    return root


def write_module(root: Path, name: str, source: str) -> None:
    """A module `glasses.<name>` in the scratch copy."""
    (root / "hermes-plugin/glasses" / f"{name}.py").write_text(source, encoding="utf-8")


def plant(root: Path, source: str) -> None:
    """Append source code to the planted core module."""
    path = root / CORE_FILE
    path.write_text(path.read_text(encoding="utf-8") + source, encoding="utf-8")


def lint(root: Path) -> subprocess.CompletedProcess[str]:
    """The real lint-imports of this venv over the scratch copy."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    argv = [str(LINT_IMPORTS), "--config", str(root / ".importlinter"), "--no-cache", "--no-logo"]
    return subprocess.run(argv, cwd=root / "hermes-plugin", env=env, capture_output=True, text=True, check=False)
