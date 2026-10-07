#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Size gate (ENGINEERING.md section 2): file and function length of tracked source files.

Files: tracked files outside .team/ named *.py *.kt *.kts *.sh *.mk *.gradle or Makefile, and Markdown under docs/.
  Hard limit 400 lines (error), target 300 (warning).
Functions: Python (ast, def/async def incl. methods, key "path::Class.name") and POSIX sh (key "path::name", rules in
  size_sh.py). Hard limit 60, target 40.
Exempt: paths matching a glob in the exempt file ("*" "?" stay in one path segment, "**" spans segments, a glob
  without "/" matches the name anywhere), and files whose leading comment header (blank/comment lines within the
  first 10) holds a marker line: comment token ("#" "//" "/*" "*" "<!--") then the word @generated. Other mentions
  (docstrings, strings, code) do not exempt.
Baseline (ratchet): "<path>:<lines> # T<id>" or "<path>::<function>:<lines> # T<id>" (id T<n>.<n>[a-z]),
  each inside a block headed "# T<id>" with the same id (a blank line ends a block). Measured length must equal the
  entry (grew -> error, shrank -> lower it); an entry within the hard limit or not measured is stale.

Usage: check_size.py [--root DIR] [--exempt FILE] [--baseline FILE]   (defaults: git top of cwd, tools/quality/*.txt)
Exit:  0 OK (warnings allowed), 1 limit or ratchet violations, 2 usage/config error.
"""

import argparse
import ast
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from size_sh import sh_functions

FILE_HARD, FILE_TARGET = 400, 300
FUNC_HARD, FUNC_TARGET = 60, 40
SOURCE_SUFFIXES = (".py", ".kt", ".kts", ".sh", ".mk", ".gradle")
SOURCE_NAMES = frozenset({"Makefile", "GNUmakefile", "makefile"})
HEAD_LINES = 10
TASK_ID = r"T\d+\.\d+[a-z]?"
BASELINE_LINE = re.compile(rf"(?P<key>\S+):(?P<limit>\d+)\s+#\s*(?P<task>{TASK_ID})(?:\s.*)?")
BLOCK_HEADER = re.compile(rf"#\s*(?P<task>{TASK_ID})\b.*")
GENERATED = re.compile(r"\s*(?:#|//|/\*+|\*|<!--)\s*@generated(?:\s.*)?")
HEADER_LINE = re.compile(r"\s*(?:#|//|/\*|\*|<!--|$)")


class ConfigError(Exception):
    """Unusable input of the checker itself (exit 2)."""


@dataclass(frozen=True)
class Item:
    key: str
    lines: int
    hard: int
    target: int


@dataclass(frozen=True)
class Entry:
    limit: int
    task: str


def count_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    if "/" not in pattern:
        pattern = "**/" + pattern
    parts = re.split(r"(\*\*/|/\*\*$|\*|\?)", pattern)
    table = {"**/": "(?:.*/)?", "/**": "/.*", "*": "[^/]*", "?": "[^/]"}
    return re.compile("".join(table.get(part, re.escape(part)) for part in parts))


def parse_exempt(text: str) -> list[re.Pattern[str]]:
    globs = (line.strip() for line in text.splitlines())
    return [glob_to_regex(g) for g in globs if g and not g.startswith("#")]


def is_exempt(path: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.fullmatch(path) for p in patterns)


def is_source(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if path.startswith("docs/") and name.endswith(".md"):
        return True
    return name in SOURCE_NAMES or name.endswith(SOURCE_SUFFIXES)


def is_generated(text: str) -> bool:
    for line in text.splitlines()[:HEAD_LINES]:
        if GENERATED.fullmatch(line):
            return True
        if not HEADER_LINE.match(line):
            return False
    return False


def parse_baseline(text: str) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    block = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            header = BLOCK_HEADER.fullmatch(line)
            block = header["task"] if header else (block if line else None)
            continue
        entry = BASELINE_LINE.fullmatch(line)
        if not entry:
            raise ConfigError(f"baseline line {number}: expected '<path>[::<function>]:<lines> # T<id>': {line}")
        if block is None:
            raise ConfigError(f"baseline line {number}: entry outside a '# T<id>' block: {line}")
        if entry["task"] != block:
            raise ConfigError(f"baseline line {number}: id {entry['task']} inside block {block}")
        if entry["key"] in entries:
            raise ConfigError(f"baseline line {number}: duplicate entry {entry['key']}")
        entries[entry["key"]] = Entry(int(entry["limit"]), entry["task"])
    return entries


def python_functions(text: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = prefix
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                end = child.end_lineno or child.lineno
                if not isinstance(child, ast.ClassDef):
                    found.append((name, end - child.lineno + 1))
                name += "."
            visit(child, name)

    visit(ast.parse(text), "")
    return found


def measure(path: str, text: str) -> list[Item]:
    items = [Item(path, count_lines(text), FILE_HARD, FILE_TARGET)]
    measurer = {".py": python_functions, ".sh": sh_functions}.get(Path(path).suffix)
    functions = measurer(text) if measurer else []
    longest: dict[str, int] = {}
    for name, length in functions:
        longest[name] = max(length, longest.get(name, 0))
    items += [Item(f"{path}::{name}", length, FUNC_HARD, FUNC_TARGET) for name, length in longest.items()]
    return items


def judge(item: Item, entry: Entry | None) -> tuple[str | None, str | None]:
    """Return (error, warning) for one measured item."""
    what = f"{item.key}: {item.lines} lines"
    if entry is None:
        if item.lines > item.hard:
            return f"{what} > hard limit {item.hard}", None
        return None, (f"{what} > target {item.target}" if item.lines > item.target else None)
    if item.lines <= item.hard:
        return f"stale baseline entry {what} ({entry.task}), within the hard limit {item.hard} - remove it", None
    if item.lines > entry.limit:
        return f"{what}, baseline {entry.task} allows {entry.limit} (grew; hard limit {item.hard})", None
    if item.lines < entry.limit:
        return f"{what}, baseline {entry.task} allows {entry.limit}: lower the entry to {item.lines}", None
    return None, None


def evaluate(items: list[Item], baseline: dict[str, Entry]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    verdicts = [judge(item, baseline.get(item.key)) for item in items]
    measured = {item.key for item in items}
    stale = [
        f"stale baseline entry {key} ({entry.task}): not measured (gone, exempt or no source)"
        for key, entry in baseline.items()
        if key not in measured
    ]
    return [error for error, _ in verdicts if error] + stale, [warning for _, warning in verdicts if warning]


def git(root: Path, *args: str) -> str:
    exe = shutil.which("git")
    if exe is None:
        raise ConfigError("git not found on PATH")
    try:
        done = subprocess.run([exe, "-C", str(root), *args], capture_output=True, text=True, check=True)  # noqa: S603 - fixed git argv
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"git {args[0]} failed in {root}: {exc}") from exc
    return done.stdout


def tracked_files(root: Path) -> list[str]:
    top = git(root, "rev-parse", "--show-toplevel").strip()
    if Path(top).resolve() != root.resolve():
        raise ConfigError(f"{root} is not the top of a git repository (git top: {top})")
    names = git(root, "-c", "core.quotePath=false", "ls-files", "-z").split("\0")
    return [n for n in names if n and not n.startswith(".team/")]


def collect(root: Path, patterns: list[re.Pattern[str]]) -> tuple[list[Item], list[str], int]:
    items: list[Item] = []
    errors: list[str] = []
    files = 0
    for path in tracked_files(root):
        full = root / path
        if not is_source(path) or is_exempt(path, patterns) or full.is_symlink() or not full.is_file():
            continue
        text = full.read_text(encoding="utf-8", errors="replace")
        if is_generated(text):
            continue
        files += 1
        try:
            items += measure(path, text)
        except (SyntaxError, ValueError) as exc:
            errors.append(f"{path}: cannot measure functions: {exc}")
    return items, errors, files


def read_config(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def run(root: Path, exempt: Path, baseline: Path) -> int:
    patterns = parse_exempt(read_config(exempt))
    entries = parse_baseline(read_config(baseline))
    items, errors, files = collect(root, patterns)
    if files == 0:
        raise ConfigError(f"no source files found under {root}")
    found, warnings = evaluate(items, entries)
    sys.stdout.writelines(f"check-size: warning: {warning}\n" for warning in warnings)
    errors += found
    sys.stderr.writelines(f"check-size: error: {error}\n" for error in errors)
    if errors:
        sys.stderr.write(f"check-size: FAILED ({len(errors)} error(s))\n")
        return 1
    functions = len(items) - files
    sys.stdout.write(f"check-size: OK ({files} files, {functions} functions, {len(warnings)} warning(s))\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="File and function length gate (ENGINEERING.md section 2).")
    parser.add_argument("--root", type=Path, help="repository top (default: git top of the current directory)")
    parser.add_argument("--exempt", type=Path, help="exempt globs (default: <root>/tools/quality/size-exempt.txt)")
    parser.add_argument("--baseline", type=Path, help="ratchet (default: <root>/tools/quality/size-baseline.txt)")
    args = parser.parse_args(argv)
    try:
        root = args.root or Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        exempt = args.exempt or root / "tools/quality/size-exempt.txt"
        return run(root, exempt, args.baseline or root / "tools/quality/size-baseline.txt")
    except ConfigError as exc:
        sys.stderr.write(f"check-size: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
