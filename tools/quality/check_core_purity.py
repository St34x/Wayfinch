#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Core purity gate, AST part (T1.100; ENGINEERING.md section 1).

The core list is the `source_modules` of the import-linter contract `core-purity` in `.importlinter` (one list, two
checks; `core_modules()` is reused by T1.101). Every file of every core module is parsed, and:
  (a) ALLOWLIST, fail closed: every import (module level or inside a function, absolute or relative) names a core
      module, `glasses.generated` (or below it), a module in `STDLIB_ALLOWLIST`, or the target of a ratchet entry
      `<this module> -> <target>` in the contract's `ignore_imports`. Everything else fails: `io`, `codecs`, `sys`,
      `logging`, any third-party or new module. `from M import x` counts as an import of `M.x` when that is a module.
  (b) I/O without an import statement: the builtins in `BANNED_NAMES` (used at all, not only called; also as a
      constant `getattr` name), clock reads (`.now`, `.utcnow`, `.today` on any object, any `time.<attr>` unless
      `time` is bound by `from datetime import time`), console streams (`.stdin`/`.stdout`/...) and submodules
      reached as attributes (`import urllib.parse` binds `urllib`; `urllib.request` must pass the allowlist too).
      Time comes into the core as a value or an injected clock.
  (c) ratchet hygiene: the nearest comment line above every `ignore_imports` entry of every contract is a
      `# T<id> ...` line (a stale entry already fails lint-imports: unmatched ignored imports are an error).
The forbidden contract in `.importlinter` stays for the transitive part (core -> generated/ratcheted module -> I/O).

Usage: check_core_purity.py [--config PATH] [--src DIR]   (defaults: <repo>/.importlinter, <repo>/hermes-plugin)
Exit:  0 clean, 1 findings (on stderr), 2 configuration error (missing file, contract or core module file).
"""

import argparse
import ast
import configparser
import functools
import importlib.util
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

TOP = Path(__file__).resolve().parents[2]
CONTRACT = "importlinter:contract:core-purity"
GENERATED = "glasses.generated"

# The ONLY stdlib modules a core module may import (exact names: a submodule needs its own line). Each one is pure
# computation on values passed in. Adding one needs a reason here and review. Left out on purpose: io, codecs, sys
# (file/console I/O), logging (handlers write to streams/files: the adapter logs what the core returns), uuid
# (uuid1/getnode read the clock and the MAC, uuid4 the OS RNG: ids come in as values), random/secrets
# (non-deterministic), time (clock), os/pathlib/socket/... (I/O).
STDLIB_ALLOWLIST: dict[str, str] = {
    "__future__": "compiler directives (annotations)",
    "abc": "abstract base classes for ports",
    "base64": "pure encoding of bytes",
    "binascii": "pure encoding of bytes",
    "collections": "containers",
    "collections.abc": "container protocols for type hints",
    "copy": "copies of values",
    "dataclasses": "value types",
    "datetime": "date/time values; reading the clock (.now/.utcnow/.today) is banned by check (b)",
    "decimal": "exact arithmetic",
    "enum": "enumerations",
    "functools": "pure higher-order helpers",
    "hashlib": "digests of bytes",
    "hmac": "MACs and constant-time compare of bytes",
    "ipaddress": "pure parsing and classification of IP address literals (no resolution)",
    "itertools": "iteration helpers",
    "json": "pure (de)serialisation of strings; files are read by adapters",
    "math": "arithmetic",
    "operator": "operator functions",
    "re": "regular expressions",
    "string": "string constants and templates",
    "textwrap": "text layout",
    "types": "type objects (MappingProxyType, SimpleNamespace)",
    "typing": "type hints",
    "unicodedata": "character properties (width, normalisation)",
    "urllib.parse": "pure URL splitting/quoting (urllib.request etc. are I/O)",
}
_BUILTIN_IO = {"open", "input", "print", "breakpoint", "exec", "eval", "compile", "__import__", "__builtins__"}
# globals/vars/locals give string-keyed access to `__builtins__` (`globals()["__builtins__"]["open"]`).
BANNED_NAMES = frozenset(_BUILTIN_IO | {"globals", "vars", "locals"})
CLOCK_ATTRS = frozenset({"now", "utcnow", "today"})
CONSOLE_ATTRS = frozenset({"stdin", "stdout", "stderr", "__stdin__", "__stdout__", "__stderr__"})
_TASK_ID_COMMENT = re.compile(r"#\s*(?:T\d+\.\d+[a-z]?|P\.\d+|S\d+[a-z]?)\b")
_CONTRACT_HEADER = re.compile(r"\[importlinter:contract:[^\]]+\]")


class PurityError(Exception):
    """The configuration cannot be checked (fail closed: exit 2)."""


@dataclass(frozen=True)
class Policy:
    """What a core module may import: the core list, the ratchet entries and the source root."""

    core: frozenset[str]
    ignored: frozenset[tuple[str, str]]
    src: Path


def _contract(config_text: str) -> configparser.SectionProxy:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(config_text)
    except configparser.Error as exc:
        raise PurityError(f"unreadable import-linter config: {exc}") from exc
    if not parser.has_section(CONTRACT):
        raise PurityError(f"section [{CONTRACT}] missing")
    return parser[CONTRACT]


def _values(config_text: str, key: str) -> list[str]:
    lines = [line.strip() for line in _contract(config_text).get(key, "").splitlines()]
    return [line for line in lines if line and not line.startswith(("#", ";"))]


def core_modules(config_text: str) -> list[str]:
    """The core list: `source_modules` of the core-purity contract (comment lines are skipped)."""
    modules = _values(config_text, "source_modules")
    if not modules:
        raise PurityError(f"[{CONTRACT}] source_modules is empty")
    return modules


def ratchet_entries(config_text: str) -> frozenset[tuple[str, str]]:
    """`ignore_imports` of the core-purity contract as (importer, imported) pairs; wildcards match nothing here."""
    pairs = [entry.partition("->") for entry in _values(config_text, "ignore_imports")]
    return frozenset((left.strip(), right.strip()) for left, sep, right in pairs if sep)


def module_files(src: Path, name: str) -> list[Path]:
    """The source files of a dotted module name below `src` (a package means all its `*.py` files)."""
    base = src.joinpath(*name.split("."))
    if base.is_dir() and (base / "__init__.py").is_file():
        return sorted(base.rglob("*.py"))
    if base.with_suffix(".py").is_file():
        return [base.with_suffix(".py")]
    raise PurityError(f"core module {name} not found below {src}")


def module_name(src: Path, path: Path) -> str:
    """Dotted name of a source file below `src` (`pkg/__init__.py` is `pkg`)."""
    parts = list(path.relative_to(src).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


@functools.cache
def _stdlib_package_dirs(top: str) -> tuple[Path, ...]:
    """Directories of a top-level stdlib package (empty for a plain module). Finds, never imports, the package."""
    if top not in sys.stdlib_module_names:
        return ()  # never look into a third-party package; its top-level name fails the allowlist anyway
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return ()
    return tuple(Path(loc) for loc in (spec.submodule_search_locations or [])) if spec else ()


def _is_module(name: str, src: Path) -> bool:
    """Whether dotted `name` is a module file: below `src` for the local root package, else in a stdlib package.

    Only files are looked at; nothing is imported (`from this import s` must not run `this`)."""
    top, *rest = name.split(".")
    roots = [src / top] if (src / top).is_dir() else list(_stdlib_package_dirs(top))
    for root in roots:
        base = root.joinpath(*rest)
        if rest and (base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()):
            return True
    return False


def _from_base(node: ast.ImportFrom, importer: str, is_package: bool) -> str:
    if node.level == 0:
        return node.module or ""
    parts = importer.split(".") if is_package else importer.split(".")[:-1]
    if node.level - 1 >= len(parts):
        return ""  # beyond the top-level package: reported as an unresolvable import
    parts = parts[: len(parts) - (node.level - 1)]
    return ".".join(parts + ([node.module] if node.module else []))


def imported_modules(node: ast.Import | ast.ImportFrom, importer: str, src: Path, is_package: bool) -> list[str]:
    """The modules an import statement imports, absolute (`from M import x` is `M.x` when that is a module)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = _from_base(node, importer, is_package)
    if not base:
        return ["." * node.level + (node.module or "")]
    subs = [f"{base}.{alias.name}" for alias in node.names if alias.name != "*"]
    return sorted({sub if _is_module(sub, src) else base for sub in subs} or {base})


def import_allowed(name: str, importer: str, policy: Policy) -> bool:
    """Allowlist (a): core module (or below a core package), generated code, allowlisted stdlib, ratchet entry."""
    in_core = any(name == core or name.startswith(core + ".") for core in policy.core)
    generated = name == GENERATED or name.startswith(GENERATED + ".")
    return in_core or generated or name in STDLIB_ALLOWLIST or (importer, name) in policy.ignored


def _module_bindings(tree: ast.Module, importer: str, src: Path, is_package: bool) -> dict[str, str]:
    """Names bound to modules by import statements, with the module: `import a.b` binds `a` (to `a`), `import a.b as n`
    binds `n` (to `a.b`), `from a import b` binds `b` (to `a.b`) when that is a module."""
    bound: dict[str, str] = {}
    for node in (n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))):
        if isinstance(node, ast.ImportFrom):
            base = _from_base(node, importer, is_package)
            subs = ((a.asname or a.name, f"{base}.{a.name}") for a in node.names if base and a.name != "*")
            bound.update((name, sub) for name, sub in subs if _is_module(sub, src))
            continue
        for alias in node.names:
            bound[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
    return bound


def _binds_datetime_time(tree: ast.Module) -> bool:
    """Whether `from datetime import time` binds `time` to the (pure) datetime.time class."""
    return any(
        node.module == "datetime" and node.level == 0 and alias.name == "time" and alias.asname is None
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )


def _attr_name(node: ast.AST) -> str | None:
    """The attribute name `node` reads: `x.attr`, or a constant string in `getattr(x, "attr")`."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
        name = node.args[1] if len(node.args) > 1 else None
        return name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else None
    return None


def _io_use(node: ast.AST, datetime_time: bool) -> str | None:
    """Check (b): I/O that needs no import statement (`datetime_time`: `time` is the datetime class here)."""
    if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
        return f"builtin {node.id}() in a core module"
    attr = _attr_name(node)
    if attr is None:
        return None
    on_time = isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "time"
    if attr in CLOCK_ATTRS or (on_time and not datetime_time):
        return f"clock read .{attr} in a core module (time comes in as a value or an injected clock)"
    if attr in CONSOLE_ATTRS:
        return f"console stream .{attr} in a core module"
    if attr in BANNED_NAMES and isinstance(node, ast.Call):
        return f"builtin {attr}() via getattr in a core module"
    return None


def _submodule_attrs(tree: ast.Module, bound: dict[str, str], src: Path) -> Iterator[tuple[ast.Attribute, str]]:
    """`<bound name>.<x>` where `<module>.<x>` is a submodule: (node, dotted submodule name)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound:
            sub = f"{bound[node.value.id]}.{node.attr}"
            if _is_module(sub, src):
                yield node, sub


def scan_source(source: str, path: Path, policy: Policy) -> list[str]:
    """Findings (a) and (b) for one core source file below `policy.src`, as `file:line: message`."""
    importer = module_name(policy.src, path)
    is_package = path.name == "__init__.py"
    tree = ast.parse(source, filename=str(path))
    bound = _module_bindings(tree, importer, policy.src, is_package)
    datetime_time = bound.get("time") is None and _binds_datetime_time(tree)
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            findings.extend(
                (node.lineno, f"import of {name} in a core module is not allowlisted (check_core_purity.py)")
                for name in imported_modules(node, importer, policy.src, is_package)
                if not import_allowed(name, importer, policy)
            )
        elif (message := _io_use(node, datetime_time)) is not None:
            findings.append((getattr(node, "lineno", 0), message))
    findings.extend(
        (node.lineno, f"module attribute {sub} in a core module is not allowlisted (check_core_purity.py)")
        for node, sub in _submodule_attrs(tree, bound, policy.src)
        if not import_allowed(sub, importer, policy)
    )
    return [f"{path}:{line}: {message}" for line, message in sorted(findings)]


def _ignore_block(lines: list[str], start: int) -> list[tuple[int, str]]:
    block: list[tuple[int, str]] = []
    inside = False
    for number, line in enumerate(lines[start + 1 :], start + 2):
        stripped = line.strip()
        if not inside:
            if stripped.startswith("["):
                break
            key, sep, value = stripped.partition("=")
            inside = bool(sep) and key.strip() == "ignore_imports"
            block += [(number, value.strip())] if inside and value.strip() else []
        elif line[:1].isspace() or stripped.startswith(("#", ";")):
            block.append((number, stripped))
        else:
            break
    return block


def ignore_entries_without_id(config_text: str) -> list[str]:
    """Finding (c): `ignore_imports` entries of any contract with no `# T<id>` comment line above them."""
    lines = config_text.splitlines()
    findings = []
    for start in (i for i, line in enumerate(lines) if _CONTRACT_HEADER.fullmatch(line.strip())):
        has_id = False
        for number, line in _ignore_block(lines, start):
            if line.startswith(("#", ";")):
                has_id = bool(_TASK_ID_COMMENT.match(line))
            elif line and not has_id:
                findings.append(f".importlinter:{number}: ignore_imports entry without a '# T<id>' line above: {line}")
    return findings


def check(config: Path, src: Path) -> list[str]:
    """All findings of the gate; raises PurityError when the configuration cannot be checked."""
    try:
        config_text = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise PurityError(f"cannot read {config}: {exc}") from exc
    core = core_modules(config_text)
    findings = ignore_entries_without_id(config_text)
    policy = Policy(frozenset(core), ratchet_entries(config_text), src)
    for name in core:
        for path in module_files(src, name):
            findings.extend(scan_source(path.read_text(encoding="utf-8"), path, policy))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Core purity gate, AST part (ENGINEERING.md section 1).")
    parser.add_argument("--config", type=Path, default=TOP / ".importlinter", help="import-linter config")
    parser.add_argument("--src", type=Path, default=TOP / "hermes-plugin", help="directory holding the root package")
    args = parser.parse_args(argv)
    try:
        findings = check(args.config, args.src)
        count = len(core_modules(args.config.read_text(encoding="utf-8")))
    except PurityError as exc:
        sys.stderr.write(f"check-core-purity: {exc}\n")
        return 2
    for finding in findings:
        sys.stderr.write(f"check-core-purity: {finding}\n")
    if findings:
        sys.stderr.write(f"check-core-purity: {len(findings)} finding(s)\n")
        return 1
    sys.stdout.write(f"check-core-purity: OK ({count} core module(s))\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
