#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Module README gate (T1.103; ENGINEERING.md section 1). Stdlib only.

Module list `MODULES_FILE` (tracked; `#` comment lines), one entry per line, fields separated by `|`:
  <dir>                        a module: <dir>/README.md is checked
  <dir> | part-of: <module>    no own README: <module> is listed above <dir> and its README names `<last segment>/`
  <dir> | length-override | reason: <text naming a specification> | ref: <doc> §<n>   (spec documents only)
<dir> = relative path of lower-case segments. Any other line, a duplicate dir or an empty list -> exit 2.
<doc> §<n> = the public rule allowing the override: a tracked file outside any dot-directory with a heading
`<n>.` (e.g. `ENGINEERING.md §1`); a ref that does not resolve is a finding.
README (tracked regular file): before its first `##` only blank lines, one-line `<!-- -->` comments (a line of whole
comments, each closed at its first `-->`; `--!>`, `<!-->` or `<!--->` make it text) and one `# ` title; the first
three `##` sections are exactly SECTIONS in order, none empty (blank lines, comments, thematic
breaks and fence lines are no content); without an override no other `##` section and at most MAX_LINES lines,
with one more than MAX_LINES (a stale override fails). Setext headings are refused; headings inside fenced code
blocks are skipped.
Required: REQUIRED_MODULES and every dir directly under a DISCOVERY_ROOTS dir holding tracked files (except
NOT_MODULES) must be listed, so neither deleting a line nor adding a tool or package skips the gate. Android modules
are added (list line + discovery root) by the task that creates them.
Usage: check_readmes.py   (from anywhere inside the repository)
Exit:  0 clean, 1 findings (stderr), 2 cannot check (git error, module list missing, untracked, empty, malformed).
"""

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MODULES_FILE = "tools/quality/modules.txt"
MAX_LINES = 40
SECTIONS = ("Purpose", "Public API", "Invariants")
REQUIRED_MODULES = ("mk", "protocol", "tools")
DISCOVERY_ROOTS = ("hermes-plugin", "tools")
NOT_MODULES = frozenset({"tests"})
SPEC_WORD = "specification"
LINE_FORMS = "'<dir>', '<dir> | part-of: <module>' or '<dir> | length-override | reason: <text> | ref: <doc> §<n>'"
RULE_DOC = "ENGINEERING.md §1"

_DIR = re.compile(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*")
_REF = re.compile(r"(?P<doc>[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*) §(?P<number>\d+)")
_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_SETEXT = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
COMMENT_OPEN = "<!--"
COMMENT_CLOSE = "-->"  # GitHub's HTML block (CommonMark type 2) ends only at this
COMMENT_BANG_CLOSE = "--!>"  # HTML parsers also end a comment here: a comment closed by it is text (fail closed)
COMMENT_ABRUPT = (">", "->")  # `<!-->` / `<!--->`: HTML parsers end the comment at once, what follows is shown
_RULE = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*")
NO_CONTENT = frozenset({"blank", "comment", "fence", "rule"})


class ReadmeError(Exception):
    """The check could not run (git failure, missing or malformed module list): exit 2, never a pass."""


@dataclass(frozen=True)
class Entry:
    path: str
    part_of: str | None = None
    ref: str | None = None  # `<doc> §<n>` of a length-override


@dataclass(frozen=True)
class Line:
    kind: str  # blank, comment, heading, setext, rule, fence, code, text
    level: int = 0
    title: str = ""


@dataclass
class Section:
    title: str
    number: int
    has_text: bool = False


def _dir(value: str, where: str) -> str:
    if not _DIR.fullmatch(value):
        raise ReadmeError(f"{where}: bad dir {value!r} (relative path, lower-case segments)")
    return value


def _field(fields: list[str], index: int, name: str) -> str:
    value = fields[index] if index < len(fields) else ""
    return value.removeprefix(name).strip() if value.startswith(name) else ""


def _override(fields: list[str], where: str) -> str:
    if len(fields) > 4:
        raise ReadmeError(f"{where}: too many fields, expected {LINE_FORMS}")
    reason = _field(fields, 2, "reason:")
    if SPEC_WORD not in reason.split():
        raise ReadmeError(f"{where}: length-override needs 'reason: <text>' naming a {SPEC_WORD} ({RULE_DOC})")
    ref = _field(fields, 3, "ref:")
    if not _REF.fullmatch(ref):
        raise ReadmeError(f"{where}: length-override needs 'ref: <doc> §<n>' (the public rule allowing it)")
    return ref


def parse_line(line: str, where: str) -> Entry:
    """One non-comment line of the module list."""
    fields = [part.strip() for part in line.split("|")]
    path = _dir(fields[0], where)
    if len(fields) == 1:
        return Entry(path)
    if len(fields) == 2 and fields[1].startswith("part-of:"):
        return Entry(path, part_of=_dir(fields[1].removeprefix("part-of:").strip(), where))
    if fields[1] == "length-override":
        return Entry(path, ref=_override(fields, where))
    raise ReadmeError(f"{where}: expected {LINE_FORMS}")


def parse_modules(text: str) -> list[Entry]:
    """All entries of the module list; duplicates or an empty list fail closed."""
    entries: list[Entry] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry = parse_line(line, f"{MODULES_FILE}:{number}")
        if any(seen.path == entry.path for seen in entries):
            raise ReadmeError(f"{MODULES_FILE}:{number}: {entry.path} listed twice")
        entries.append(entry)
    if not entries:
        raise ReadmeError(f"{MODULES_FILE}: no modules listed")
    return entries


def _skip_blanks(text: str, at: int) -> int:
    """Index of the first character at or after `at` that is not a space or tab."""
    while at < len(text) and text[at] in " \t":
        at += 1
    return at


def _comment_end(text: str, start: int) -> int:
    """Index just past the `-->` closing the comment whose body starts at `start`; -1 when the comment does not
    close cleanly there: no `-->`, an abrupt `<!-->`/`<!--->` (HTML shows what follows) or a `--!>` before the
    `-->` (HTML ends the comment there, GitHub's block parser only at `-->`). Searches only from `start` on."""
    if text.startswith(COMMENT_ABRUPT, start):
        return -1
    close = text.find(COMMENT_CLOSE, start)
    if close < 0 or text.find(COMMENT_BANG_CLOSE, start, close) >= 0:
        return -1
    return close + len(COMMENT_CLOSE)


def is_comment_line(line: str) -> bool:
    """Only spaces/tabs and whole HTML comments, each closed cleanly at its first `-->` (see `_comment_end`), so no
    text between or after comments passes as a comment and anything an HTML parser or GitHub would read differently
    is text. One index walk, every search starts at the current position: linear in the line length. Scanned by
    hand: no regex parses HTML here."""
    at = _skip_blanks(line, 0)
    if at == len(line):
        return False
    while at < len(line):
        if not line.startswith(COMMENT_OPEN, at):
            return False
        at = _comment_end(line, at + len(COMMENT_OPEN))
        if at < 0:
            return False
        at = _skip_blanks(line, at)
    return True


def _kind(line: str, previous: str) -> Line:
    if not line.strip():
        return Line("blank")
    if is_comment_line(line):
        return Line("comment")
    heading = _HEADING.fullmatch(line)
    if heading:
        return Line("heading", len(heading[1]), (heading[2] or "").strip())
    if previous == "text" and _SETEXT.fullmatch(line):
        return Line("setext")
    return Line("rule") if _RULE.fullmatch(line) else Line("text")


def classify(lines: list[str]) -> list[Line]:
    """Kind of every Markdown line; fence lines are `fence`, the lines between them `code`."""
    out: list[Line] = []
    fence = ""
    for line in lines:
        stripped = line.strip()
        if fence:
            if stripped.startswith(fence) and set(stripped) == {fence[0]}:
                fence = ""
                out.append(Line("fence"))
            else:
                out.append(Line("code"))
            continue
        opened = _FENCE.match(line)
        if opened:
            fence = opened[1]
            out.append(Line("fence"))
            continue
        out.append(_kind(line, out[-1].kind if out else "blank"))
    return out


def _line_problem(number: int, line: Line, sections: list[Section], titles: int) -> str | None:
    if line.kind == "setext":
        return f"line {number}: setext heading (underline) - use `#` headings"
    if sections:
        return f"line {number}: `# ` title after the first `##` section" if line.level == 1 else None
    title_ok = line.kind == "heading" and (line.level == 2 or (line.level == 1 and not titles))
    if title_ok or line.kind in {"blank", "comment"}:
        return None
    return f"line {number}: only a `# ` title, blank lines and comments may precede `## {SECTIONS[0]}`"


def outline(lines: list[Line]) -> tuple[list[Section], list[str]]:
    """The `##` sections and the structural problems of the lines before and between them."""
    sections: list[Section] = []
    problems: list[str] = []
    titles = 0
    for number, line in enumerate(lines, 1):
        problem = _line_problem(number, line, sections, titles)
        if problem:
            problems.append(problem)
        titles += line.kind == "heading" and line.level == 1
        if line.kind == "heading" and line.level == 2:
            sections.append(Section(line.title, number))
        elif sections and line.kind not in NO_CONTENT:
            sections[-1].has_text = True
    return sections, problems


def check_readme(text: str, *, spec: bool) -> list[str]:
    """Problems of one README; `spec` = it has a length-override line."""
    lines = classify(text.splitlines())
    sections, problems = outline(lines)
    titles = [section.title for section in sections]
    if titles[: len(SECTIONS)] != list(SECTIONS):
        problems.append(f"the first `##` sections must be exactly {', '.join(SECTIONS)} (found {titles[:3]})")
    problems += [f"line {s.number}: `## {s.title}` is empty" for s in sections[: len(SECTIONS)] if not s.has_text]
    if spec and len(lines) <= MAX_LINES:
        problems.append(f"{len(lines)} lines: the length-override is not needed, remove its line from {MODULES_FILE}")
    if not spec:
        problems += [f"line {s.number}: extra section `## {s.title}`" for s in sections[len(SECTIONS) :]]
        if len(lines) > MAX_LINES:
            problems.append(
                f"{len(lines)} lines, limit {MAX_LINES} (a spec document needs a length-override, {RULE_DOC})"
            )
    return problems


def _git(top: Path | None, *args: str) -> str:
    command = ["git", *(["-C", str(top)] if top is not None else []), *args]
    try:
        done = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            command, capture_output=True, encoding="utf-8", errors="surrogateescape", check=False
        )
    except OSError as exc:
        raise ReadmeError(f"cannot run git: {exc}") from exc
    if done.returncode != 0:
        raise ReadmeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _git_files(top: Path) -> set[str]:
    if Path(_git(top, "rev-parse", "--show-toplevel").strip()).resolve() != top.resolve():
        raise ReadmeError(f"{top}: not the top level of a git repository")
    names = {name for name in _git(top, "-c", "core.quotePath=false", "ls-files", "-z").split("\0") if name}
    if not names:
        raise ReadmeError("no tracked files")
    return names


def _read(top: Path, name: str) -> str | None:
    path = top / name
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
    except UnicodeDecodeError:
        return None


def ref_findings(top: Path, entry: Entry, tracked: set[str]) -> list[str]:
    """A length-override ref names a numbered section of a tracked document (`_REF` admits no dot-directory)."""
    found = _REF.fullmatch(entry.ref or "")
    if found is None:
        return []
    doc = found["doc"]
    text = _read(top, doc) if doc in tracked else None
    if text is None:
        return [f"{entry.path}: ref {entry.ref}: {doc} is not a tracked public file"]
    if not re.search(rf"^#{{1,6}}[ \t]+{found['number']}\.[ \t]", text, re.MULTILINE):
        return [f"{entry.path}: ref {entry.ref}: {doc} has no heading `{found['number']}.`"]
    return []


def required(tracked: set[str]) -> set[str]:
    """`REQUIRED_MODULES` plus the directories directly under a discovery root that hold tracked files."""
    found = set(REQUIRED_MODULES)
    for name in tracked:
        parts = PurePosixPath(name).parts
        if len(parts) >= 3 and parts[0] in DISCOVERY_ROOTS and parts[1] not in NOT_MODULES:
            found.add(f"{parts[0]}/{parts[1]}")
    return found


def _part_of_findings(top: Path, entry: Entry, modules: dict[str, Entry]) -> list[str]:
    parent = modules.get(entry.part_of or "")
    if parent is None or parent.part_of is not None or not entry.path.startswith(f"{parent.path}/"):
        return [f"{entry.path}: part-of {entry.part_of} is not a listed module above it with its own README"]
    name = f"`{PurePosixPath(entry.path).name}/`"
    if name not in (_read(top, f"{parent.path}/README.md") or ""):
        return [f"{parent.path}/README.md: does not name its part {name}"]
    return []


def _entry_findings(top: Path, entry: Entry, modules: dict[str, Entry], tracked: set[str]) -> list[str]:
    if not any(name.startswith(f"{entry.path}/") for name in tracked):
        return [f"{entry.path}: listed in {MODULES_FILE} but holds no tracked file"]
    if entry.part_of is not None:
        return _part_of_findings(top, entry, modules)
    readme = f"{entry.path}/README.md"
    text = _read(top, readme) if readme in tracked else None
    if text is None:
        return [f"{readme}: missing, untracked, a symlink or not UTF-8"]
    problems = [f"{readme}: {problem}" for problem in check_readme(text, spec=entry.ref is not None)]
    return problems + ref_findings(top, entry, tracked)


def check_tree(top: Path) -> tuple[list[str], int]:
    """Findings of every listed module plus the unlisted required dirs; also the number of READMEs checked."""
    tracked = _git_files(top)
    text = _read(top, MODULES_FILE) if MODULES_FILE in tracked else None
    if text is None:
        raise ReadmeError(f"{MODULES_FILE}: missing, untracked or unreadable")
    modules = {entry.path: entry for entry in parse_modules(text)}
    findings = [
        f"{path}: module dir not in {MODULES_FILE} (give it a README and a line, or a part-of line)"
        for path in sorted(required(tracked) - modules.keys())
    ]
    for entry in modules.values():
        findings += _entry_findings(top, entry, modules, tracked)
    return findings, sum(entry.part_of is None for entry in modules.values())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        sys.stderr.write("usage: check_readmes.py   (no arguments)\n")
        return 2
    try:
        top = Path(_git(None, "rev-parse", "--show-toplevel").strip())
        findings, count = check_tree(top)
    except ReadmeError as exc:
        sys.stderr.write(f"check-readmes: {exc}\n")
        return 2
    for finding in findings:
        sys.stderr.write(f"check-readmes: {finding}\n")
    if findings:
        sys.stderr.write(f"check-readmes: {len(findings)} finding(s) in {MODULES_FILE} modules\n")
        return 1
    sys.stdout.write(f"check-readmes: OK ({count} module READMEs, {MODULES_FILE})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
