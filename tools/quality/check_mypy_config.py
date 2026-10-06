#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Config gate of mypy --strict (T1.99, ENGINEERING.md sections 5 and 10): nothing may weaken --strict.

mypy.ini (read with configparser.RawConfigParser, as mypy reads it) may hold only these sections, each with exactly
the listed keys and values:
  [mypy]                    GLOBAL (strict, Python floor 3.11, scope, cache, warn_unused_ignores, ignore-without-code)
  the Hermes boundary       HERMES: ignore_missing_imports = True
  [mypy-glasses.adapter]    ADAPTER: disallow_subclassing_any = False (the adapter subclasses an Any base)
  baseline sections         ignore_errors = True, inside a block: a "# T<id>" header line, its sections, a blank line.
Baseline ids and their module globs must be a subset of RATCHET: a task deletes its own block when it types the
module, and no block or section is ever added.
Checked files (*.py, *.pyi under the [mypy] files) may not hold mypy inline configuration comments, a type-ignore
comment on a line of its own (mypy reads one before the first statement as "ignore this whole module"), or the
typing decorator that switches checking of a function off. Code mypy never reads fails too: a stub (*.pyi; mypy
checks it instead of the .py next to it), a file its directory crawl skips (see CRAWL_SKIPS), and code behind a name
mypy treats as a constant (FLAGS: `if not TYPE_CHECKING:`, an else after `if TYPE_CHECKING:`, `MYPY`, `PY2`).

Usage: check_mypy_config.py [--root DIR]   (default: the repository holding this script)
Exit:  0 OK, 1 violations, 2 mypy.ini or a checked file unreadable (not UTF-8).
"""

import argparse
import ast
import configparser
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

GLOBAL = {
    "strict": "True",
    "warn_unused_ignores": "True",
    "enable_error_code": "ignore-without-code",
    "python_version": "3.11",
    "files": "hermes-plugin/glasses, tools",
    "cache_dir": ".local/mypy-cache",
    "namespace_packages": "False",
}
HERMES = "mypy-gateway.*,hermes_cli.*,tools.*,agent.*,hermes_constants"
ADAPTER = "mypy-glasses.adapter"
FIXED = {
    "mypy": GLOBAL,
    HERMES: {"ignore_missing_imports": "True"},
    ADAPTER: {"disallow_subclassing_any": "False"},
}
BASELINE_KEYS = {"ignore_errors": "True"}
RATCHET: Mapping[str, tuple[str, ...]] = {
    "T1.111": ("glasses.config",),
    "T1.112": ("mock_llm",),
    "T1.113": ("glasses", "glasses.adapter.*", "glasses.gate"),
    "T1.55": ("glasses_min", "glasses_min.adapter"),
}

HEADER = re.compile(r"^# (T\d+\.\d+[a-z]?)$")
SECTION = re.compile(r"^\[(?P<name>.+)\]")  # configparser.RawConfigParser.SECTCRE
# The patterns are split so that this file does not match them itself.
SUPPRESSIONS = {
    re.compile("#" + r"\s*mypy\s*:", re.IGNORECASE): "mypy inline configuration comment",
    re.compile(r"^\s*#\s*type\s*:\s*" + "ignore"): "type-ignore comment on a line of its own",
    re.compile(r"\bno_type_" + "check"): "typing decorator that switches checking off",
}
# Names mypy's reachability analysis treats as constants (mypy/reachability.py): MYPY and TYPE_CHECKING are true for
# mypy and false at runtime, PY2 is always false and PY3 always true. Code mypy then calls unreachable is never
# checked, although it may run. The one allowed use: `if TYPE_CHECKING:` (or `typing.TYPE_CHECKING`) as the whole
# test of an if without else/elif whose body holds only imports (a `raise` there would hide the rest of the module).
FLAGS = frozenset({"TYPE_CHECKING", "MYPY", "PY2", "PY3"})
FLAG_MSG = "TYPE_CHECKING/MYPY/PY2/PY3 test hides code from mypy (allowed: imports-only `if TYPE_CHECKING:`, no else)"
# Directory and file names mypy's directory crawl skips (mypy/find_sources.py), besides every name starting with ".".
CRAWL_SKIPS = frozenset({"__pycache__", "site-packages", "node_modules"})


class ConfigError(Exception):
    """mypy.ini or a checked file cannot be read."""


def globs(section: str) -> list[str]:
    """Module globs of a `[mypy-...]` section name (comma-separated, as mypy splits them)."""
    return [glob.strip() for glob in section.removeprefix("mypy-").split(",")]


def parse_blocks(text: str) -> tuple[dict[str, list[str]], list[str]]:
    """Return (follow-up id -> section names of its baseline block, layout errors)."""
    blocks: dict[str, list[str]] = {}
    errors: list[str] = []
    block: str | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        task = HEADER.match(line)
        section = SECTION.match(line)
        if not line:
            block = None
        elif task and block is not None:
            errors.append(f"line {number}: block {block} needs a blank line before the next header")
        elif task and task.group(1) in blocks:
            errors.append(f"line {number}: second block for {task.group(1)}")
        elif task:
            block = task.group(1)
            blocks[block] = []
        elif section and block is not None:
            blocks[block].append(section.group("name"))
    return blocks, errors


def _ratchet_errors(blocks: Mapping[str, list[str]], ratchet: Mapping[str, tuple[str, ...]]) -> list[str]:
    errors: list[str] = []
    for task, sections in blocks.items():
        if not sections:
            errors.append(f"block {task} has no section")
        if task not in ratchet:
            errors.append(f"block {task} is not in the baseline (new code never gets a baseline block)")
            continue
        extra = [glob for section in sections for glob in globs(section) if glob not in ratchet[task]]
        if extra:
            errors.append(f"block {task}: {extra} not in its baseline (the baseline only shrinks)")
    return errors


def _diff(want: Mapping[str, str], got: Mapping[str, str]) -> str:
    wrong = [f"{key} = {got[key]} (allowed: {want[key]})" for key in want if key in got and got[key] != want[key]]
    extra = [f"extra key {key} = {value}" for key, value in got.items() if key not in want]
    missing = [f"missing {key} = {value}" for key, value in want.items() if key not in got]
    return "; ".join(wrong + extra + missing)


def _section_errors(parser: configparser.RawConfigParser, owners: Mapping[str, str]) -> list[str]:
    errors = [] if "mypy" in parser else ["missing [mypy] section"]
    for name in parser.sections():
        want = FIXED.get(name) or (BASELINE_KEYS if name in owners else None)
        if want is None:
            errors.append(f"[{name}]: unexpected section (outside a '# T<id>' baseline block)")
            continue
        got = dict(parser.items(name))
        if got != want:
            errors.append(f"[{name}] must hold exactly its allowed keys: {_diff(want, got)}")
    return errors


def check_config(text: str, ratchet: Mapping[str, tuple[str, ...]] = RATCHET) -> list[str]:
    """Violations of the allowed mypy.ini layout; ConfigError if configparser cannot read it."""
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise ConfigError(str(exc).replace("\n", " ")) from exc
    blocks, errors = parse_blocks(text)
    owners = {section: task for task, sections in blocks.items() for section in sections}
    return errors + _ratchet_errors(blocks, ratchet) + _section_errors(parser, owners)


def checked_files(root: Path) -> list[Path]:
    """The *.py/*.pyi files under the GLOBAL `files` directories (the ones mypy skips fail in `unread_files`)."""
    found: set[Path] = set()
    for folder in globs(GLOBAL["files"]):
        found.update(path for path in (root / folder).rglob("*.py*") if path.suffix in {".py", ".pyi"})
    return sorted(found)


def unread_files(root: Path, files: Iterable[Path]) -> list[str]:
    """Checked files whose code mypy never reads: stubs and files its directory crawl skips."""
    errors: list[str] = []
    for path in files:
        where = path.relative_to(root)
        if path.suffix == ".pyi":
            errors.append(f"{where}: stub file (mypy checks a .pyi instead of its .py; no stubs in the checked tree)")
        elif any(part in CRAWL_SKIPS or part.startswith(".") for part in where.parts):
            errors.append(f"{where}: mypy's directory crawl skips this path (hidden or {sorted(CRAWL_SKIPS)} name)")
        elif path.with_suffix("").is_dir():
            errors.append(f"{where}: mypy's directory crawl skips a .py next to a directory of the same name")
    return errors


def _flag(node: ast.AST) -> str | None:
    """The FLAGS name `node` refers to (`NAME` or `anything.NAME`), else None."""
    if isinstance(node, ast.Name):
        return node.id if node.id in FLAGS else None
    if isinstance(node, ast.Attribute):
        return node.attr if node.attr in FLAGS else None
    return None


def hidden_code(tree: ast.AST) -> list[int]:
    """Lines where a FLAGS name hides code from mypy: every use except an imports-only `if TYPE_CHECKING:`."""
    allowed: set[int] = set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _flag(node.test) == "TYPE_CHECKING" and not node.orelse:
            allowed.add(id(node.test))
            if not all(isinstance(statement, ast.Import | ast.ImportFrom) for statement in node.body):
                lines.add(node.lineno)
    lines.update(
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.expr) and _flag(node) and id(node) not in allowed
    )
    return sorted(lines)


def _findings(text: str) -> list[tuple[int, str]]:
    """(line, what) of every suppression and hidden-code construct in the source `text`."""
    found = [
        (number, what)
        for number, line in enumerate(text.splitlines(), 1)
        for pattern, what in SUPPRESSIONS.items()
        if pattern.search(line)
    ]
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return [*found, (getattr(exc, "lineno", None) or 1, f"not valid Python: {exc}")]
    return found + [(number, FLAG_MSG) for number in hidden_code(tree)]


def scan(root: Path, files: Iterable[Path]) -> list[str]:
    """Suppressions and hidden code in `files`, as "path:line: what" relative to `root`; ConfigError if not UTF-8."""
    found: list[str] = []
    for path in files:
        where = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{where}: not UTF-8 ({exc.reason} at byte {exc.start})") from exc
        found += [f"{where}:{number}: {what}" for number, what in _findings(text)]
    return found


def run(root: Path) -> int:
    try:
        text = (root / "mypy.ini").read_text(encoding="utf-8")
        errors = check_config(text)
    except (OSError, ConfigError) as exc:
        sys.stderr.write(f"check-mypy-config: cannot read mypy.ini: {exc}\n")
        return 2
    files = checked_files(root)
    if not files:
        errors.append(f"no checked files found under {GLOBAL['files']}")
    try:
        errors += unread_files(root, files) + scan(root, files)
    except (OSError, ConfigError) as exc:
        sys.stderr.write(f"check-mypy-config: cannot read a checked file: {exc}\n")
        return 2
    for error in errors:
        sys.stderr.write(f"check-mypy-config: {error}\n")
    if errors:
        return 1
    sys.stdout.write(f"check-mypy-config: OK (mypy.ini layout, {len(files)} checked files without suppressions)\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mypy.ini layout and inline-suppression gate (T1.99).")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2], help="repository top")
    return run(parser.parse_args(argv).root)


if __name__ == "__main__":
    sys.exit(main())
