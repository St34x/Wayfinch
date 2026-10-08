#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Dependency ledger gate (T1.136, ENGINEERING.md sections 8 and 10): docs/dependencies.md matches the pins.

What must agree (names compared PEP 503-normalised: PyYAML = pyyaml, mypy_extensions = mypy-extensions):
  requirements-dev.txt  one `# direct: <name> (<scope>)` header line per direct requirement (scope: SCOPES) and one
                        exact `<name>==<version>` line per pinned package, direct or transitive;
  docs/dependencies.md  "## Python": one row per direct requirement, Version = its pin, Scope = its header scope, and
                        no other row; "## Tool binaries": a row per tool in TOOLS at the version of its source of
                        truth, and the platform digest table equal to it; "## Licence exceptions": every name is
                        pinned; every "all <N> pins" statement (the licence check) has N = the number of pins;
  tools/secret-scan.sh  GITLEAKS_VERSION=<version>; pinned_sha256()'s `case "$1" in` holds only `<platform>) echo
                        <sha256> ;;` arms and a final `*) return 1 ;;`; every other line is a finding unless it is
                        blank or a comment as sh sees it (lines split on LF only; blanks are space and tab only).
Limits: whether a pin is direct is declared by people (the `# direct:` lines) and checked in review, not derived from
the pin graph. Only the first `pinned_sha256() {` case table is read, not how the script uses it: a second definition,
the table in a heredoc, `WANT=` reassigned after the call or a shadowed `echo` are not detected (code review).

Usage: check_ledger.py [--root DIR]   (default: the repository holding this script)
Exit:  0 OK; 1 findings, one `file:line: what` per line on stderr; 2 cannot check (a file missing or not UTF-8, a
       section, table, column, table row or the pin count statement missing, no direct line, no pin, no digest
       `case` block, no digest).
"""

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

from ledger_tables import LEDGER, CannotCheckError, Table, read, table, tables

REQS = "requirements-dev.txt"
SCAN = "tools/secret-scan.sh"
SCOPES = frozenset({"runtime", "dev"})
TOOLS = frozenset({"gitleaks"})  # tool binaries whose source of truth this gate reads (tools/secret-scan.sh)

NAME = r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
DIRECT_MARK = re.compile(r"^#\s*direct\b")
DIRECT = re.compile(rf"^# direct: {NAME} \((?P<value>[a-z]+)\)$")
PIN = re.compile(rf"^{NAME}==(?P<value>[A-Za-z0-9.+!_-]+)(?:\s+\\)?$")
HASH = re.compile(r"^\s+--hash=\S+(?:\s+\\)?$")  # a continuation line of the pin above
GITLEAKS_VERSION = re.compile(r"^GITLEAKS_VERSION=(?P<value>\S+)$")
DIGEST = re.compile(r"[ \t]*(?P<name>[a-z0-9_]+)\) echo (?P<value>[0-9a-f]{64}) ;;")  # all four: fullmatch
DIGEST_FUNCTION = re.compile(r"pinned_sha256\(\) \{")
CASE_START = re.compile(r'[ \t]*case "\$1" in')
CASE_END = re.compile(r"[ \t]*esac")
DEFAULT_ARM = re.compile(r"[ \t]*\*\) return 1 ;;")
SH_BLANK_OR_COMMENT = re.compile(r"[ \t]*(?:#.*)?")  # sh's blanks are space and tab only
UNEXPECTED_ARM = "unexpected case arm in pinned_sha256 (only '<platform>) echo <sha256> ;;' lines are checked)"
PIN_COUNT = re.compile(r"\ball\s+(\d+)\s+pins\b")
FORMS = {"direct requirement": "`# direct: <name> (<scope>)`", "pin": "<name>==<version>"}
COUNT_FIX = (
    "check the licences of the added/removed pins, then update this sentence; a new direct requirement also needs a "
    f"`# direct: <name> (<scope>)` line in {REQS} and a ## Python row"
)


class Entry(NamedTuple):
    line: int
    name: str
    value: str


class Finding(NamedTuple):
    file: str
    line: int
    what: str


def norm(name: str) -> str:
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _record(into: dict[str, Entry], number: int, match: re.Match[str] | None, what: str) -> list[Finding]:
    if match is None:
        return [Finding(REQS, number, f"malformed {what} line (expected {FORMS[what]})")]
    key = norm(match["name"])
    if key in into:
        return [Finding(REQS, number, f"{match['name']}: duplicate {what} (first at line {into[key].line})")]
    into[key] = Entry(number, match["name"], match["value"])
    return []


def parse_reqs(text: str) -> tuple[dict[str, Entry], dict[str, Entry], list[Finding]]:
    """Direct requirements and pins by normalised name, plus malformed or duplicate lines."""
    direct: dict[str, Entry] = {}
    pins: dict[str, Entry] = {}
    found: list[Finding] = []
    continued = False  # the previous physical line ends with `\`: only then may an indented `--hash=` line follow
    for number, line in enumerate(text.splitlines(), 1):
        if DIRECT_MARK.match(line):
            found += _record(direct, number, DIRECT.match(line), "direct requirement")
        elif line[:1].isspace() and line.strip() and not (continued and HASH.match(line)):
            found.append(Finding(REQS, number, f"malformed pin line (expected {FORMS['pin']}, unindented)"))
        elif line and not line[0].isspace() and not line.startswith("#"):
            found += _record(pins, number, PIN.match(line), "pin")
        continued = line.endswith("\\")
    if not direct:
        raise CannotCheckError(f"{REQS}: no `# direct: <name> (<scope>)` line")
    if not pins:
        raise CannotCheckError(f"{REQS}: no `<name>==<version>` pin")
    return direct, pins, found


def _case_block(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """The non-blank, non-comment lines between `case "$1" in` and `esac` right inside `pinned_sha256() {`."""
    state = "function"  # then "case" (the line after the header must open the case), then "arms"
    block: list[tuple[int, str]] = []
    for number, line in lines:
        if state == "function":
            state = "case" if DIGEST_FUNCTION.fullmatch(line) else state
        elif state == "case":
            if not CASE_START.fullmatch(line):
                break
            state = "arms"
        elif CASE_END.fullmatch(line):
            return [(n, ln) for n, ln in block if not SH_BLANK_OR_COMMENT.fullmatch(ln)]
        else:
            block.append((number, line))
    raise CannotCheckError(f'{SCAN}: no `pinned_sha256() {{` + `case "$1" in` ... `esac` block')


def _digest_arms(block: list[tuple[int, str]]) -> tuple[dict[str, Entry], list[Finding]]:
    """Digest per platform (the first arm, as the shell `case` takes it); any other arm but a final default fails."""
    digests: dict[str, Entry] = {}
    found: list[Finding] = []
    for index, (number, line) in enumerate(block):
        match = DIGEST.fullmatch(line)
        if match is None:
            if index != len(block) - 1 or not DEFAULT_ARM.fullmatch(line):
                found.append(Finding(SCAN, number, UNEXPECTED_ARM))
        elif (first := digests.get(match["name"])) is not None:
            what = f"gitleaks {first.name}: duplicate platform (first at line {first.line})"
            found.append(Finding(SCAN, number, what))
        else:
            digests[match["name"]] = Entry(number, match["name"], match["value"])
    return digests, found


def parse_scan(text: str) -> tuple[Entry, dict[str, Entry], list[Finding]]:
    """The gitleaks version, the archive digest per platform and the unexpected lines of the digest `case`."""
    lines = list(enumerate(text.split("\n"), 1))  # LF only, as sh: CR, VT, FF, U+001C-U+001E, U+0085 stay text
    versions = [Entry(n, "gitleaks", m["value"]) for n, ln in lines if (m := GITLEAKS_VERSION.match(ln))]
    digests, found = _digest_arms(_case_block(lines))
    if len(versions) != 1:
        raise CannotCheckError(f"{SCAN}: no GITLEAKS_VERSION=<version> line (or more than one)")
    if not digests:
        raise CannotCheckError(f"{SCAN}: no `<platform>) echo <sha256> ;;` line")
    return versions[0], digests, found


def _check_row(row: Entry, scope: str, direct: dict[str, Entry], pins: dict[str, Entry]) -> list[Finding]:
    found: list[Finding] = []
    header, pin = direct.get(norm(row.name)), pins.get(norm(row.name))
    if header is None:
        found.append(Finding(LEDGER, row.line, f"{row.name}: row without a `# direct:` line in {REQS}"))
    elif scope != header.value:
        found.append(
            Finding(
                LEDGER, row.line, f"{row.name}: scope {scope} in the ledger, {header.value} in {REQS}:{header.line}"
            )
        )
    if pin is None:
        found.append(Finding(LEDGER, row.line, f"{row.name}: row without a pin in {REQS}"))
    elif row.value != pin.value:
        found.append(
            Finding(
                LEDGER,
                row.line,
                f"{row.name}: version {row.value} in the ledger, pinned {pin.value} ({REQS}:{pin.line})",
            )
        )
    return found


def _check_direct(entry: Entry, pinned: bool, has_row: bool) -> list[Finding]:
    found: list[Finding] = []
    if entry.value not in SCOPES:
        found.append(
            Finding(REQS, entry.line, f"{entry.name}: scope {entry.value!r} is not one of: {', '.join(sorted(SCOPES))}")
        )
    if not pinned:
        found.append(Finding(REQS, entry.line, f"{entry.name}: direct requirement is not pinned"))
    if not has_row:
        found.append(
            Finding(REQS, entry.line, f"{entry.name}: direct requirement without a row in {LEDGER} (## Python)")
        )
    return found


def check_python(python: Table, direct: dict[str, Entry], pins: dict[str, Entry]) -> list[Finding]:
    """Rows of `## Python` against the direct requirements and their pins, both ways."""
    found: list[Finding] = []
    rows: dict[str, int] = {}
    for number, cells in python.rows:
        row = Entry(number, python.cell(cells, "Name"), python.cell(cells, "Version"))
        if norm(row.name) in rows:
            found.append(Finding(LEDGER, number, f"{row.name}: duplicate row (first at line {rows[norm(row.name)]})"))
            continue
        rows[norm(row.name)] = number
        found += _check_row(row, python.cell(cells, "Scope"), direct, pins)
    for key, entry in direct.items():
        found += _check_direct(entry, key in pins, key in rows)
    return found


def check_tools(tools: Table, version: Entry, scan: dict[str, Entry], digests: Table) -> list[Finding]:
    """The tool rows and the platform digest table against tools/secret-scan.sh."""
    found: list[Finding] = []
    for number, cells in tools.rows:
        name, listed = tools.cell(cells, "Name"), tools.cell(cells, "Version")
        if name not in TOOLS:
            found.append(
                Finding(
                    LEDGER, number, f"{name}: tool binary without a known source of truth ({', '.join(sorted(TOOLS))})"
                )
            )
        elif listed != version.value:
            found.append(
                Finding(
                    LEDGER, number, f"{name}: version {listed} in the ledger, {version.value} in {SCAN}:{version.line}"
                )
            )
    if not any(tools.cell(cells, "Name") == version.name for _, cells in tools.rows):
        found.append(Finding(SCAN, version.line, f"{version.name}: no row in {LEDGER} (## {tools.section})"))
    platforms = set()
    for number, cells in digests.rows:
        platform, digest = digests.cell(cells, "Platform"), digests.cell(cells, "Archive SHA-256")
        platforms.add(platform)
        pinned = scan.get(platform)
        if pinned is None:
            found.append(Finding(LEDGER, number, f"gitleaks {platform}: platform not pinned in {SCAN}"))
        elif digest != pinned.value:
            found.append(Finding(LEDGER, number, f"gitleaks {platform}: digest differs from {SCAN}:{pinned.line}"))
    missing = [entry for platform, entry in scan.items() if platform not in platforms]
    return found + [Finding(SCAN, e.line, f"gitleaks {e.name}: digest missing from {LEDGER}") for e in missing]


def check_ledger_text(text: str, exceptions: Table, pins: dict[str, Entry]) -> list[Finding]:
    """Licence exceptions name pinned packages; every "all <N> pins" statement counts the pins."""
    found = [
        Finding(LEDGER, number, f"{name}: licence exception names a package that is not pinned")
        for number, cells in exceptions.rows
        if norm(name := exceptions.cell(cells, "Name")) not in pins
    ]
    counts = list(PIN_COUNT.finditer(text))
    if not counts:
        raise CannotCheckError(f"{LEDGER}: no 'all <N> pins' statement (the licence check's scope)")
    for match in counts:
        if int(match[1]) != len(pins):
            stale = f"the ledger states all {match[1]} pins, {REQS} has {len(pins)}"
            found.append(Finding(LEDGER, text.count("\n", 0, match.start()) + 1, f"{stale}: {COUNT_FIX}"))
    return found


def check(root: Path) -> tuple[list[Finding], str]:
    """All findings over the three files under `root`, and the OK summary."""
    direct, pins, found = parse_reqs(read(root, REQS))
    version, scan, scan_found = parse_scan(read(root, SCAN))
    found += scan_found
    text = read(root, LEDGER)
    all_tables = tables(text)
    python = table(all_tables, "Python", ("Version", "Name", "Scope"))
    tools = table(all_tables, "Tool binaries", ("Version", "Name"))
    digests = table(all_tables, "Tool binaries", ("Platform", "Archive SHA-256"))
    exceptions = table(all_tables, "Licence exceptions", ("Licence", "Name"))
    found += check_python(python, direct, pins) + check_tools(tools, version, scan, digests)
    found += check_ledger_text(text, exceptions, pins)
    summary = (
        f"{len(direct)} direct requirements = {len(python.rows)} rows at their pins, {len(pins)} pins, "
        f"gitleaks {version.value} + {len(scan)} digests, {len(exceptions.rows)} licence exceptions pinned"
    )
    return found, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dependency ledger gate (T1.136).")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2], help="repository top")
    try:
        found, summary = check(parser.parse_args(argv).root)
    except CannotCheckError as exc:
        sys.stderr.write(f"check-ledger: cannot check: {exc}\n")
        return 2
    for finding in sorted(found):
        sys.stderr.write(f"{finding.file}:{finding.line}: {finding.what}\n")
    if found:
        sys.stderr.write(f"check-ledger: {len(found)} finding(s)\n")
        return 1
    sys.stdout.write(f"check-ledger: OK ({summary})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
