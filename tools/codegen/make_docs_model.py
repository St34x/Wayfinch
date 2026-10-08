# SPDX-License-Identifier: Apache-2.0
"""Pure parsing and rules behind gen_make_docs.py (no I/O: callers pass file texts and get findings back).

- Make files: every literal rule target, the `#: <target> — <description>` line(s) directly above its rule, the
  `TEST_TARGETS` entries and the literal prerequisites of the `test` rule.
- `ENGINEERING.md` section 10 gate table: per row its line, state (first word of the State cell) and targets. A code
  span in the Make target cell holding one target-like word is a make target; a span with spaces is a command run
  outside make (`check_hygiene.py --commits`) and is not a target; a cell with no code span at all is a finding.
- Markdown: `make <target>` commands in code spans and fenced code blocks. Only `make` in command position counts (the
  start of a span, of a fenced line or of a `;`/`&&`/`||`/`|`/`(` segment, after a `$` prompt and `VAR=value` words);
  `-f`/`-C`/`-I`/`-o`/`-W` take a value, `VAR=value`, other options, placeholders (`<target>`) and numbers are skipped,
  and a `#` comment or a redirection ends the command. Prose ("make this work") is never read. Known gap: a make behind
  a command prefix (`sudo`/`env`/`time`/`nohup make x`) and indented code blocks or `<code>` are not read.
- Make lines may be indented with spaces (only a tab starts a recipe); one `override`/`export` word before
  `TEST_TARGETS` is allowed.
"""

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

ENTRY = "test"
TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
DESCRIPTION = re.compile(r"#: (?P<name>\S+) — (?P<text>\S.*)")
RULE = re.compile(r" *(?P<names>[^\s#:=][^#:=]*?)\s*::?(?![:=])(?P<rest>.*)")
TEST_TARGETS = re.compile(r" *(?:(?:override|export)\s+)?TEST_TARGETS\s*(?:\+=|::?=|\?=|=)(?P<value>.*)")
SEPARATOR_ROW = re.compile(r"\|(?:\s*:?-{3,}:?\s*\|)+")
DIRECTIVES = frozenset({"ifeq", "ifneq", "ifdef", "ifndef", "else", "endif", "include", "-include", "sinclude"})
DIRECTIVES_MORE = frozenset({"export", "unexport", "override", "define", "endef", "vpath", "undefine"})
VALUE_OPTIONS = frozenset({"-f", "-C", "-I", "-o", "-W", "--file", "--makefile", "--directory"})
FENCE = re.compile(r" {0,3}(?P<mark>`{3,}|~{3,})(?P<info>.*)")
SPAN = re.compile(r"(?<!`)(?P<ticks>`+)(?!`)(?P<code>.+?)(?<!`)(?P=ticks)(?!`)", re.S)
SEPARATORS = re.compile(r"&&|\|\||[;|()]")


class ModelError(Exception):
    """An input the checker cannot judge (exit 2), e.g. no section 10 gate table."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Target:
    name: str
    path: str
    line: int
    description: str = ""


@dataclass
class Inventory:
    """Everything the make files define: targets by name, `make test` entries (name, path, line), findings."""

    targets: dict[str, Target] = field(default_factory=dict)
    entries: list[tuple[str, str, int]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


@dataclass(frozen=True)
class GateRow:
    line: int
    state: str
    targets: tuple[str, ...]


def logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """(first line number, line) with backslash continuations joined."""
    start, parts = 0, []
    for number, line in enumerate(text.splitlines(), 1):
        start = start or number
        if line.endswith("\\"):
            parts.append(line[:-1])
            continue
        yield start, "".join([*parts, line])
        start, parts = 0, []
    if parts:
        yield start, "".join(parts)


def rule_targets(line: str) -> tuple[list[str], str] | None:
    """Target names (special `.TARGETS` dropped) and prerequisites of a rule line, None when the line is no rule."""
    match = RULE.match(line)
    if match is None or line.split()[0] in DIRECTIVES | DIRECTIVES_MORE:
        return None
    return [name for name in match["names"].split() if not name.startswith(".")], match["rest"]


class _FileParser:
    """One make file into an Inventory: `#:` lines must sit directly above a rule of the target they name."""

    def __init__(self, path: str, inventory: Inventory) -> None:
        self.path, self.inv = path, inventory
        self.pending: list[tuple[int, str, str]] = []

    def _find(self, line: int, message: str) -> None:
        self.inv.findings.append(Finding(self.path, line, message))

    def feed(self, number: int, line: str) -> None:
        if line.startswith("#:"):
            self._description(number, line)
            return
        rule = rule_targets(line)
        if rule is not None:
            self._rule(number, *rule)
        self.flush(rule[0] if rule else [])
        assignment = TEST_TARGETS.match(line)
        if assignment:
            self._entries(number, assignment["value"].split())

    def _description(self, number: int, line: str) -> None:
        match = DESCRIPTION.fullmatch(line.rstrip())
        if match is None:
            self._find(number, "malformed `#:` line; the format is `#: <target> — <description>`")
        else:
            self.pending.append((number, match["name"], match["text"]))

    def flush(self, names: Sequence[str]) -> None:
        """Attach pending `#:` lines to the rule names just read (none = not directly above a rule)."""
        for number, name, text in self.pending:
            if name not in names or name not in self.inv.targets:
                self._find(number, f"`#: {name}` is not directly above a rule of `{name}`")
            elif self.inv.targets[name].description:
                self._find(number, f"`{name}` already has a `#:` description")
            else:
                old = self.inv.targets[name]
                self.inv.targets[name] = Target(name, old.path, old.line, text)
        self.pending = []

    def _rule(self, number: int, names: list[str], prerequisites: str) -> None:
        for name in names:
            if not TARGET_NAME.fullmatch(name):
                self._find(number, f"rule target `{name}` is not a literal name; the reference cannot list it")
            elif name not in self.inv.targets:
                self.inv.targets[name] = Target(name, self.path, number)
        if names == [ENTRY]:
            self._entries(number, prerequisites.split(), literal_only=True)

    def _entries(self, number: int, words: Iterable[str], literal_only: bool = False) -> None:
        for word in words:
            if TARGET_NAME.fullmatch(word):
                self.inv.entries.append((word, self.path, number))
            elif not literal_only:
                self._find(number, f"TEST_TARGETS entry `{word}` is not a literal target name")


def collect(files: Sequence[tuple[str, str]]) -> Inventory:
    """Parse (path, text) make files in include order; a defined target without a `#:` line is a finding."""
    inventory = Inventory()
    for path, text in files:
        parser = _FileParser(path, inventory)
        for number, line in logical_lines(text):
            parser.feed(number, line)
        parser.flush([])
    for target in inventory.targets.values():
        if not target.description:
            inventory.findings.append(Finding(target.path, target.line, f"target `{target.name}` has no `#:` line"))
    for name, where, at in inventory.entries:
        if name not in inventory.targets:
            inventory.findings.append(Finding(where, at, f"`make test` runs `{name}`, which no rule defines"))
    return inventory


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def _section_table(text: str) -> list[tuple[int, str]]:
    """(line number, line) of the table rows in section 10 (raises ModelError when there is no section 10)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## 10.")), None)
    if start is None:
        raise ModelError("no `## 10.` section (gate table)")
    rows = []
    for number, line in enumerate(lines[start + 1 :], start + 2):
        if line.startswith("## "):
            break
        if line.startswith("|"):
            rows.append((number, line))
    return rows


def gate_rows(text: str, path: str) -> tuple[list[GateRow], list[Finding]]:
    """The section 10 gate table rows and the findings on their form (raises ModelError without a usable table)."""
    rows = _section_table(text)
    header = _cells(rows[0][1]) if rows else []
    if "Make target" not in header or "State" not in header:
        raise ModelError("section 10 has no table with `Make target` and `State` columns")
    if len(rows) < 2 or not SEPARATOR_ROW.fullmatch(rows[1][1].strip()):
        raise ModelError("section 10 gate table has no `|---|` separator row under its header")
    column, state = header.index("Make target"), header.index("State")
    result, findings = [], []
    for number, line in rows[2:]:
        cells = _cells(line)
        if len(cells) != len(header):
            findings.append(Finding(path, number, f"row has {len(cells)} cells, the header {len(header)}"))
            continue
        spans = re.findall(r"`([^`]+)`", cells[column])
        word = (cells[state].split() or [""])[0]
        if not spans:
            findings.append(Finding(path, number, "Make target cell names no target in a code span"))
        if word not in {"live", "planned"}:
            findings.append(Finding(path, number, "State must start with `live` or `planned`"))
        result.append(GateRow(number, word, tuple(span for span in spans if TARGET_NAME.fullmatch(span))))
    return result, findings


def table_findings(inventory: Inventory, rows: Sequence[GateRow], path: str) -> list[Finding]:
    """Every `make test` entry sits in a live row; every target of a live row is defined."""
    live = {target for row in rows if row.state == "live" for target in row.targets}
    findings = [
        Finding(where, line, f"`make test` runs `{name}`, but no live row of {path} section 10 names it")
        for name, where, line in inventory.entries
        if name not in live
    ]
    for row in rows:
        if row.state == "live":
            findings += [
                Finding(path, row.line, f"live row names `{name}`, which no rule defines")
                for name in row.targets
                if name not in inventory.targets
            ]
    return findings


def _fenced_and_prose(text: str) -> tuple[list[tuple[int, str]], str]:
    """Lines inside fenced code blocks, and the rest of the text with those blocks blanked (line numbers kept)."""
    fenced: list[tuple[int, str]] = []
    prose: list[str] = []
    mark = ""
    for number, line in enumerate(text.splitlines(), 1):
        opener = FENCE.match(line)
        closes = bool(opener and mark and opener["mark"].startswith(mark) and not opener["info"].strip())
        if mark and not closes:
            fenced.append((number, line))
        elif closes:
            mark = ""
        elif opener:
            mark = opener["mark"]
        prose.append("" if mark or opener else line)
    return fenced, "\n".join(prose)


def _make_words(segment: str) -> list[str]:
    """The arguments of a `make` command in command position ([] when the segment is no make command)."""
    words = segment.split()
    while words and (words[0] == "$" or ("=" in words[0] and words[0] != "make")):
        words.pop(0)
    return words[1:] if words[:1] == ["make"] else []


def make_targets(command: str) -> list[str]:
    """Target names a shell command line passes to make."""
    names: list[str] = []
    for segment in SEPARATORS.split(command):
        skip = False
        for word in _make_words(segment):
            if word.startswith("#") or ">" in word:
                break
            if not skip and TARGET_NAME.fullmatch(word) and not word.isdigit():
                names.append(word)
            skip = not skip and word in VALUE_OPTIONS
    return names


def commands(text: str) -> Iterator[tuple[int, str]]:
    """(line, target) for every make target named in a code span or a fenced code block line of Markdown text."""
    fenced, prose = _fenced_and_prose(text)
    for number, line in fenced:
        for name in make_targets(line):
            yield number, name
    for match in SPAN.finditer(prose):
        if "\n\n" not in match["code"]:
            for name in make_targets(" ".join(match["code"].split())):
                yield prose.count("\n", 0, match.start("code")) + 1, name
