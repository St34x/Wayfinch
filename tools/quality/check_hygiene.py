#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Repository hygiene gate (T1.102; ENGINEERING.md sections 3, 4 and 9).

Tree mode (default, part of `make test` via mk/hygiene.mk):
  (a) every task-id marker keyword (the three words matched by `_KEYWORD`) inside a comment of a tracked file
      outside `.team/` must be written `TODO(<task id>)`; ids are `T<n>.<n>[a-z]` (letter = optional sub-id such
      as `T1.06a`), `P.<n>` or `S<nn>[a-z]`.
      Only comment text is matched: `#` / `//` / `/*` / a leading `*` at the start of a line or after
      whitespace, and `<!-- ... -->` in Markdown/XML/HTML. Words inside code, strings, JSON and Markdown prose
      are not comments; `\\uXXXX` or `mktemp ...XXXXXX` never match (whole words only). Known limitation: Python
      docstrings are not comments, and a `#` after whitespace inside a string literal is taken as one.
  (c) docs/glossary.md: every table term is defined once and no two terms share a definition
      (case-, whitespace- and markup-insensitive).
  (d) no private reference in any tracked text file outside `.team/` (the team folder is never published, so a
      reader could not follow it): no path into the team folder (`_PRIVATE_FOLDER`; only the gate files in
      `PRIVATE_FOLDER_EXEMPT`, which must name the folder to skip it), and nowhere a decision-record number
      (`adr`, an optional `-`, `_`, `/`, `#` or space, then digits) or a QA/review report name (`qa` or
      `review`, `-` or `_`, a task id), see `_PRIVATE_DOC`; and no review-process tag (`_PROCESS_TAG`): a QA wave,
      finding or round id (`qa` + `w`/`s`/`r` + digits), a review/QA/fix round number, a wave number, a
      `round <n>` fix/finding/xfail, lists or decisions of the team lead; any case, `-`/`_`/space separators. Cite the
      public document that carries the reasoning (ENGINEERING.md, docs/, protocol/README.md) or state the
      behaviour inline. Whole file text and every tracked path name are matched, not only comments; a file
      name with a `_qa`/`-qa<n>` suffix before its extension is a finding too (`_PROCESS_NAME`).
Commit mode (`--commits [RANGE]`, run by CI):
  (b) every commit subject in the range starts with `<task id>: ` or a lowercase area prefix (`SUBJECT_PREFIXES`)
      followed by text. Without RANGE the range is `<gate start>..HEAD`: the gate start is the commit hash in
      `GATE_START_FILE` (the merge commit of T1.102 on main, written there by the merge step in a follow-up commit;
      older commits are exempt history). CI runs `check_hygiene.py --commits`. An explicit RANGE is checked as given.

Usage: check_hygiene.py [--commits [RANGE]]   (from anywhere inside the repository)
Exit:  0 clean, 1 findings (on stderr), 2 usage or git error (fails closed: no repository, unreadable index,
       bad range, a range with an empty side such as `..HEAD`, no commits in the range, gate start file missing,
       empty or not a commit).
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

TASK_ID = r"T\d+\.\d+[a-z]?|P\.\d+|S\d+[a-z]?"
SUBJECT_PREFIXES = (
    "merge", "design", "docs", "plan", "tasks", "adr", "research", "spike", "mockups", "qa", "review", "status",
    "night", "chore", "fix", "feat", "test", "refactor",
)  # fmt: skip
GLOSSARY = "docs/glossary.md"
GATE_START_FILE = "tools/quality/commit-gate-start.txt"
GATE_RANGE = "GATE_START..HEAD"
PRIVATE_DIR = ".team/"
# Gate code (and its tests) that must name the private folder to leave it out of scope. Nothing else may.
PRIVATE_FOLDER_EXEMPT = frozenset({
    "tools/check-spdx.sh",
    "tools/tests/test_check_spdx.sh",
    "tools/tests/test_check_spdx_edges.sh",
    "tools/quality/check_size.py",
    "tools/quality/tests/test_check_size.py",
    "tools/quality/check_hygiene.py",
    "tools/quality/tests/test_check_hygiene_cli.py",
})
BINARY_PROBE = 8192

_KEYWORD = re.compile(r"\b(?:TODO|FIXME|XXX)\b")
_ID_SUFFIX = re.compile(rf"\((?:{TASK_ID})\)")
_SUBJECT = re.compile(rf"(?:{TASK_ID}|{'|'.join(SUBJECT_PREFIXES)}): \S")
_HASH = r"(?:^|(?<=\s))#"
_SLASH = r"(?:^|(?<=\s))(?://|/\*)|^\s*\*"
_HASH_MARKER = re.compile(_HASH)
_SLASH_MARKER = re.compile(_SLASH)
_ANY_MARKER = re.compile(f"{_HASH}|{_SLASH}")
_HTML_COMMENT = re.compile(r"<!--(.*?)(?:-->|\Z)", re.DOTALL)
_HASH_SUFFIXES = frozenset({".py", ".sh", ".mk", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".properties", ".txt"})
_SLASH_SUFFIXES = frozenset({".kt", ".kts", ".gradle", ".java", ".js", ".ts", ".c", ".h", ".cpp", ".css", ".groovy"})
_HTML_SUFFIXES = frozenset({".md", ".xml", ".html", ".svg"})
_NO_COMMENT_SUFFIXES = frozenset({".json"})
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_SEPARATOR_CELL = re.compile(r":?-+:?")
_RANGE_DOTS = re.compile(r"\.\.\.?")
_COMMIT_HASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PRIVATE_FOLDER = re.compile(r"\.team\b", re.IGNORECASE)
_PROCESS_TAG = (
    r"(?<![a-z0-9])(?:qa|review|fix)[-_ ]?(?:rounds?[-_ ]?|r)\d|(?<![a-z0-9])qa[-_ ]?[ws]\d"
    r"|(?<![a-z0-9])wave[-_ ]?\d{1,2}[a-z]?(?![a-z0-9])"
    r"|(?<![a-z0-9])round[-_ ]?\d+[-_ ](?:fix(?:es)?|findings?|xfails?)(?![a-z0-9])"
    r"|(?<![a-z0-9])lead(?:'s|\u2019s|s)?[-_ ](?:list|decisions?)(?![a-z0-9])"
)
_PRIVATE_DOC = re.compile(rf"\badr[-_/# ]{{0,2}}\d+|\b(?:qa|review)[-_](?:{TASK_ID})\b|{_PROCESS_TAG}", re.IGNORECASE)
_PROCESS_NAME = re.compile(r"[-_]qa\d*\.[^./]+$", re.IGNORECASE)


class HygieneError(Exception):
    """The check could not run (git failure, bad argument, nothing to check): exit 2, never a pass."""


# --- pure checks -------------------------------------------------------------------------------------------------


def _line_marker(path: str) -> re.Pattern[str] | None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _NO_COMMENT_SUFFIXES:
        return None
    if suffix in _HASH_SUFFIXES or PurePosixPath(path).name in {"Makefile", "GNUmakefile", "makefile"}:
        return _HASH_MARKER
    if suffix in _SLASH_SUFFIXES:
        return _SLASH_MARKER
    return _ANY_MARKER


def comment_texts(path: str, text: str) -> list[tuple[int, str]]:
    """(line number, comment text) for every comment line of `text`, by the comment syntax of `path`."""
    if PurePosixPath(path).suffix.lower() in _HTML_SUFFIXES:
        found: list[tuple[int, str]] = []
        for match in _HTML_COMMENT.finditer(text):
            first = text.count("\n", 0, match.start(1)) + 1
            found.extend((first + i, part) for i, part in enumerate(match.group(1).split("\n")))
        return found
    marker = _line_marker(path)
    if marker is None:
        return []
    return [(n, line[m.end() :]) for n, line in enumerate(text.splitlines(), 1) if (m := marker.search(line))]


def todo_findings(path: str, text: str) -> list[str]:
    """Findings for marker keywords in comments that are not written `TODO(<task id>)`."""
    findings = []
    for lineno, comment in comment_texts(path, text):
        for word in _KEYWORD.finditer(comment):
            if word.group() == "TODO" and _ID_SUFFIX.match(comment, word.end()):
                continue
            findings.append(f"{path}:{lineno}: {word.group()} without a task id (write TODO(T<id>): ...)")
    return findings


def private_ref_findings(path: str, text: str) -> list[str]:
    """Findings for references a reader of the published tree cannot follow (rule (d) above)."""
    patterns = [_PRIVATE_DOC] if path in PRIVATE_FOLDER_EXEMPT else [_PRIVATE_FOLDER, _PRIVATE_DOC]
    return [
        f"{path}:{lineno}: private reference {hit.group()!r} (cite a public doc or explain inline)"
        for lineno, line in enumerate(text.splitlines(), 1)
        for pattern in patterns
        for hit in pattern.finditer(line)
    ]


def private_name_findings(path: str) -> list[str]:
    """Findings for a tracked path name that itself is a private reference (rule (d), e.g. a copied report)."""
    return [
        f"{path}: private reference {hit.group()!r} in the path name (rename the file)"
        for pattern in (_PRIVATE_FOLDER, _PRIVATE_DOC, _PROCESS_NAME)
        for hit in pattern.finditer(path)
    ]


def subject_ok(subject: str) -> bool:
    """True if a commit subject starts with `<task id>: ` or `<area prefix>: ` followed by text."""
    return _SUBJECT.match(subject) is not None


def parse_gate_start(text: str) -> str | None:
    """The commit hash in the gate start file, or None if it holds none yet (blank and `#` lines only)."""
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return None
    if len(lines) > 1 or not _COMMIT_HASH.fullmatch(lines[0]):
        raise HygieneError(f"{GATE_START_FILE}: expected one full commit hash, got {lines!r}")
    return lines[0]


def check_range_syntax(revision_range: str) -> None:
    """Reject ranges that git would read as options or as an implied HEAD (`..HEAD`, `HEAD..`, `..`, `...HEAD`)."""
    if not revision_range or revision_range.startswith("-"):
        raise HygieneError(f"bad --commits range: {revision_range!r}")
    sides = _RANGE_DOTS.split(revision_range)
    if len(sides) > 1 and not all(side.strip() for side in sides):
        raise HygieneError(f"--commits range {revision_range!r} has an empty side (unset CI variable?)")


def _cells(line: str) -> list[str] | None:
    row = line.strip()
    if not row.startswith("|"):
        return None
    row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip() for cell in _CELL_SPLIT.split(row)]


def _is_separator(cells: list[str] | None) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL.fullmatch(cell) for cell in cells or [])


def _table_rows(text: str) -> list[tuple[int, list[str]]]:
    """Body rows (line number, cells) of every Markdown table; header and separator rows are skipped."""
    lines = text.splitlines()
    rows = []
    for index, line in enumerate(lines):
        cells = _cells(line)
        if cells is None or _is_separator(cells):
            continue
        if index + 1 < len(lines) and _is_separator(_cells(lines[index + 1])):
            continue
        rows.append((index + 1, cells))
    return rows


def _normal(text: str) -> str:
    return " ".join(text.replace("**", "").replace("`", "").lower().split()).rstrip(".")


def glossary_findings(text: str, path: str = GLOSSARY) -> list[str]:
    """Findings for a term defined twice or two terms with the same definition in the glossary tables."""
    rows = [(n, cells) for n, cells in _table_rows(text) if len(cells) >= 2]
    if not rows:
        return [f"{path}: no glossary table rows found"]
    terms: dict[str, int] = {}
    meanings: dict[str, int] = {}
    findings = []
    for lineno, cells in rows:
        term, meaning = _normal(cells[0]), _normal(cells[1])
        if term in terms:
            findings.append(f"{path}:{lineno}: term '{term}' already defined on line {terms[term]}")
        terms.setdefault(term, lineno)
        if meaning in meanings:
            findings.append(f"{path}:{lineno}: same definition as line {meanings[meaning]}")
        meanings.setdefault(meaning, lineno)
    return findings


# --- git and file access -----------------------------------------------------------------------------------------


def _git(top: Path | None, *args: str) -> str:
    command = ["git", *(["-C", str(top)] if top is not None else []), *args]
    try:
        done = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            command, capture_output=True, encoding="utf-8", errors="surrogateescape", check=False
        )
    except OSError as exc:
        raise HygieneError(f"cannot run git: {exc}") from exc
    if done.returncode != 0:
        raise HygieneError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout


def _read_text(path: Path) -> str | None:
    """File text, or None for symlinks, deleted files and binary files (a NUL byte near the start)."""
    if path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data[:BINARY_PROBE]:
        return None
    return data.decode("utf-8", errors="replace")


def check_tree(top: Path) -> tuple[list[str], str]:
    """Markers and private references in every tracked text file outside `.team/`, then the glossary."""
    names = [name for name in _git(top, "ls-files", "-z").split("\0") if name]
    findings: list[str] = []
    checked = 0
    for name in names:
        if name.startswith(PRIVATE_DIR):
            continue
        findings += private_name_findings(name)
        text = _read_text(top / name)
        if text is not None:
            checked += 1
            findings += todo_findings(name, text) + private_ref_findings(name, text)
    if checked == 0:
        raise HygieneError("no tracked text files to check")
    glossary = _read_text(top / GLOSSARY)
    findings += [f"{GLOSSARY}: missing"] if glossary is None else glossary_findings(glossary)
    return findings, f"{checked} files, {GLOSSARY}"


def gate_start(top: Path) -> str:
    """The recorded gate start commit; missing, empty or unknown fails closed."""
    path = top / GATE_START_FILE
    text = _read_text(path)
    if text is None:
        raise HygieneError(f"{GATE_START_FILE}: missing or unreadable")
    start = parse_gate_start(text)
    if start is None:
        raise HygieneError(f"{GATE_START_FILE}: no commit recorded yet (the merge step of T1.102 writes it)")
    _git(top, "rev-parse", "--quiet", "--verify", "--end-of-options", f"{start}^{{commit}}")
    return start


def check_commits(top: Path, revision_range: str) -> tuple[list[str], str]:
    """Subjects of every commit in `revision_range` (git log syntax, e.g. `origin/main..HEAD`, or GATE_RANGE)."""
    if revision_range == GATE_RANGE:
        revision_range = f"{gate_start(top)}..HEAD"
    check_range_syntax(revision_range)
    log = _git(top, "log", "--format=%h%x09%s", "--end-of-options", revision_range, "--")
    commits = [line.split("\t", 1) for line in log.split("\n") if line]
    if not commits:
        raise HygieneError(f"no commits in {revision_range} (nothing to check)")
    findings = [
        f"commit {sha}: subject {subject!r} must start with '<task id>: ' or '<area>: '"
        for sha, subject in commits
        if not subject_ok(subject)
    ]
    count = len(commits)
    return findings, f"{count} commit{'' if count == 1 else 's'} in {revision_range}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repository hygiene gate (ENGINEERING.md sections 3, 4, 9).")
    parser.add_argument(
        "--commits",
        nargs="?",
        const=GATE_RANGE,
        metavar="RANGE",
        help=f"check commit subjects of a git range instead of the tree (default {GATE_RANGE}, see {GATE_START_FILE})",
    )
    args = parser.parse_args(argv)
    try:
        top = Path(_git(None, "rev-parse", "--show-toplevel").strip())
        findings, scope = check_tree(top) if args.commits is None else check_commits(top, args.commits)
    except HygieneError as exc:
        sys.stderr.write(f"check-hygiene: {exc}\n")
        return 2
    for finding in findings:
        sys.stderr.write(f"check-hygiene: {finding}\n")
    if findings:
        sys.stderr.write(f"check-hygiene: {len(findings)} finding(s) ({scope})\n")
        return 1
    sys.stdout.write(f"check-hygiene: OK ({scope})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
