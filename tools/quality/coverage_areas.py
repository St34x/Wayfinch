#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Coverage gate (T1.101; ENGINEERING.md section 6), run by `make coverage-py` after the plugin tests ran under
coverage with `.coveragerc` (the recipe deletes the previous run's data first, so no data = exit 2).

Areas: core = the files of the `source_modules` of the core-purity contract in `.importlinter` (read with
check_core_purity.core_modules: one list, two checks); rest = every other `*.py` below `hermes-plugin/glasses`
except the generated directory. Checks, all fail closed:
  (a) `.coveragerc` holds exactly the keys and values in EXPECTED plus the ratchet section (nothing can be excluded,
      omitted or switched off there);
  (b) the measured file set equals the files on disk (an unmeasured file is an error, not a pass);
  (c) every file reaches its area minimum (core 90, rest 70; percent rounded down, statements + branches) or has a
      ratchet entry equal to its measured percentage; an entry for a file at the minimum or not measured is stale;
  (d) each area as a whole: `coverage report --include=<area files> --fail-under=<minimum>`.

Usage: coverage_areas.py [--top DIR]   (default: the repository of this script)
Exit:  0 clean, 1 findings (on stderr), 2 configuration error (missing file/config/data, unexpected key, unmeasured).
"""

import argparse
import configparser
import json
import math
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from check_core_purity import PurityError, core_modules, module_files

TOP = Path(__file__).resolve().parents[2]
SRC = "hermes-plugin"
PACKAGE = "hermes-plugin/glasses"
GENERATED = "hermes-plugin/glasses/generated"
CORE_MIN = 90
REST_MIN = 70
RATCHET = "wayfinch:coverage-ratchet"
EXPECTED: dict[str, dict[str, str]] = {
    "run": {
        "branch": "True",
        "source": PACKAGE,
        "omit": f"{GENERATED}/*",
        "relative_files": "True",
        "data_file": ".local/coverage-py/.coverage",
    },
    "report": {"exclude_lines": "", "partial_branches": ""},
}
_ID_LINE = re.compile(r"#\s*(T\d+\.\d+[a-z]?|P\.\d+|S\d+[a-z]?)\b.*")
_ENTRY = re.compile(r"(\S+)\s*=\s*(\d{1,3})")


class GateError(Exception):
    """The gate cannot judge the tree (fail closed: exit 2)."""


@dataclass(frozen=True)
class Entry:
    """One ratchet entry: the exact measured percentage a file below its minimum must keep, and the task fixing it."""

    percent: int
    task: str


def config_findings(text: str) -> list[str]:
    """Every section/key of `.coveragerc` that differs from EXPECTED (the ratchet section is parsed separately)."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise GateError(f".coveragerc unreadable: {exc}") from exc
    allowed = {*EXPECTED, RATCHET}
    findings = [f".coveragerc: section [{name}] not allowed" for name in parser.sections() if name not in allowed]
    for name, keys in EXPECTED.items():
        found = {key: value.strip() for key, value in parser.items(name)} if parser.has_section(name) else {}
        if found != keys:
            findings.append(f".coveragerc: [{name}] must be exactly {keys}, found {found}")
    return findings


def _ratchet_lines(text: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = lines.index(f"[{RATCHET}]")
    except ValueError as exc:
        raise GateError(f".coveragerc: section [{RATCHET}] missing") from exc
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    return lines[start + 1 : end]


def ratchet(text: str) -> dict[str, Entry]:
    """Ratchet entries by file; every entry sits in a block under a `# T<id>` header line (blank line ends a block)."""
    entries: dict[str, Entry] = {}
    task: str | None = None
    for line in (raw.strip() for raw in _ratchet_lines(text)):
        header = _ID_LINE.fullmatch(line)
        if header:
            task = header.group(1)
        elif not line:
            task = None
        if not line or line.startswith("#"):
            continue
        entry = _ENTRY.fullmatch(line)
        if entry is None or task is None:
            raise GateError(f".coveragerc: ratchet line not '<file> = <percent>' under a '# T<id>' line: {line}")
        if entry.group(1) in entries:
            raise GateError(f".coveragerc: duplicate ratchet entry {entry.group(1)}")
        entries[entry.group(1)] = Entry(int(entry.group(2)), task)
    return entries


def areas(top: Path, importlinter_text: str) -> tuple[list[str], list[str]]:
    """(core files, rest files) as repository-relative paths; empty package or a core file outside it fails."""
    measured = sorted(
        path.relative_to(top).as_posix()
        for path in (top / PACKAGE).rglob("*.py")
        if not path.is_relative_to(top / GENERATED)
    )
    if not measured:
        raise GateError(f"no *.py below {PACKAGE}")
    try:
        names = core_modules(importlinter_text)
        core = sorted({p.relative_to(top).as_posix() for name in names for p in module_files(top / SRC, name)})
    except PurityError as exc:
        raise GateError(f".importlinter: {exc}") from exc
    outside = [path for path in core if path not in measured]
    if outside:
        raise GateError(f"core files not measured (generated or outside {PACKAGE}): {outside}")
    return core, [path for path in measured if path not in core]


def _coverage(top: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "coverage", *args, "--rcfile", str(top / ".coveragerc")]
    return subprocess.run(argv, cwd=top, capture_output=True, text=True, check=False)  # noqa: S603 - fixed argv


def measured_percent(top: Path) -> dict[str, int]:
    """Percent (rounded down) per file from the coverage data of the last run; no data fails."""
    done = _coverage(top, "json", "-q", "-o", "-")
    if done.returncode != 0:
        detail = done.stderr.strip() or done.stdout.strip()
        raise GateError(f"coverage json failed (run the tests under coverage first): {detail}")
    files = json.loads(done.stdout)["files"]
    return {path: math.floor(data["summary"]["percent_covered"]) for path, data in files.items()}


def judge(path: str, percent: int, minimum: int, entry: Entry | None) -> str | None:
    """Finding for one file, or None."""
    what = f"{path}: {percent} % (minimum {minimum})"
    if entry is None:
        return f"{what} - below the minimum" if percent < minimum else None
    if percent >= minimum:
        return f"stale ratchet entry {path} = {entry.percent} ({entry.task}): {what} - remove the entry"
    if percent != entry.percent:
        return f"{what}, ratchet {entry.task} says {entry.percent}: set the entry to {percent} or restore the tests"
    return None


def file_findings(percents: dict[str, int], minimums: dict[str, int], entries: dict[str, Entry]) -> list[str]:
    """Checks (b) and (c); a measured set different from the files on disk is a configuration error."""
    if set(percents) != set(minimums):
        missing, extra = sorted(set(minimums) - set(percents)), sorted(set(percents) - set(minimums))
        raise GateError(f"measured files differ from the plugin files: unmeasured {missing}, unexpected {extra}")
    findings = [judge(path, percents[path], minimums[path], entries.get(path)) for path in sorted(minimums)]
    stale = [
        f"stale ratchet entry {path} ({entry.task}): not measured"
        for path, entry in sorted(entries.items())
        if path not in minimums
    ]
    return [item for item in findings if item] + stale


def area_findings(top: Path, core: list[str], rest: list[str]) -> list[str]:
    """Check (d): one `coverage report --fail-under` run per area (an empty rest area is skipped)."""
    findings = []
    for label, files, minimum in (("core", core, CORE_MIN), ("rest", rest, REST_MIN)):
        if not files:
            continue
        done = _coverage(top, "report", f"--include={','.join(files)}", f"--fail-under={minimum}")
        sys.stdout.write(f"coverage-py: {label} area (minimum {minimum} %)\n{done.stdout.rstrip()}\n")
        if done.returncode != 0:
            findings.append(f"{label} area below {minimum} %: {done.stderr.strip() or done.stdout.splitlines()[-1]}")
    return findings


def check(top: Path) -> list[str]:
    """All findings for the tree at `top`; raises GateError when it cannot be judged."""
    try:
        rc_text = (top / ".coveragerc").read_text(encoding="utf-8")
        importlinter_text = (top / ".importlinter").read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"config missing: {exc}") from exc
    findings = config_findings(rc_text)
    entries = ratchet(rc_text)
    core, rest = areas(top, importlinter_text)
    if findings:
        return findings
    minimums = {path: CORE_MIN for path in core} | {path: REST_MIN for path in rest}
    findings = file_findings(measured_percent(top), minimums, entries)
    return findings + area_findings(top, core, rest)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--top", type=Path, default=TOP, help="repository root")
    args = parser.parse_args(argv)
    try:
        findings = check(args.top.resolve())
    except GateError as exc:
        sys.stderr.write(f"coverage-py: error: {exc}\n")
        return 2
    sys.stderr.writelines(f"coverage-py: {finding}\n" for finding in findings)
    if findings:
        return 1
    sys.stdout.write("coverage-py: OK\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
