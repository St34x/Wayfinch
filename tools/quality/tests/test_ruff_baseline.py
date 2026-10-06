# SPDX-License-Identifier: Apache-2.0
"""Ruff gate (T1.97): the ratchet baseline in ruff.toml and planted violations.

The baseline is the part of ``[lint.per-file-ignores]`` between ``# BEGIN ratchet baseline`` and
``# END ratchet baseline``: one block per follow-up id (``# T<id>`` header, entries ending in the same ``# T<id>``,
a blank line). Every listed code must still fire in its file (no stale entry), and every violation in the tree
must be covered by the test policy or the baseline. Planted code goes through stdin, so nothing is written.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "ruff.toml"
LINT_DIRS = ("hermes-plugin", "protocol", "tools")
BEGIN, END = "# BEGIN ratchet baseline", "# END ratchet baseline"
TASK_ID = r"T\d+\.\d+[a-z]?"
HEADER_RE = re.compile(rf"# (?P<id>{TASK_ID})")
ENTRY_RE = re.compile(rf'"(?P<path>[^"*?\[\]]+)" = \[(?P<codes>[^\]]*)\]  # (?P<id>{TASK_ID})')
CODE_RE = re.compile(r'"([A-Z]+[0-9]+)"')
TEST_POLICY = {"**/tests/**": ["S101", "PLR0913", "PLR0917", "S104", "S105", "S603", "S607"]}
CORE_FILE = "hermes-plugin/glasses/_planted.py"
# The gate passes the root config explicitly (mk/lint-py.mk), so nested ruff configs never apply.
GATE_CONFIG = ("--config", "ruff.toml")
# Pinned rule set and limits (ENGINEERING.md sections 2, 3, 5): dropping a family or loosening a limit fails here.
SELECT = ["E", "F", "W", "B", "UP", "SIM", "BLE", "S", "T20", "ERA", "I", "C901", "PLR0913", "PLR0917", "PLR1702"]
SELECT += ["PGH004", "RUF100"]
TOP_LEVEL = {
    "target-version": "py311",
    "line-length": 120,
    "preview": True,
    "output-prefer-rule-codes": True,
    "force-exclude": True,
    "extend-exclude": ["hermes-plugin/glasses/generated/**"],
}
LINT_KEYS = {"explicit-preview-rules", "select", "mccabe", "pylint", "isort", "per-file-ignores"}
LIMITS = {"mccabe": {"max-complexity": 10}, "pylint": {"max-args": 4, "max-positional-args": 4, "max-nested-blocks": 3}}


class BaselineError(ValueError):
    """The baseline block of ruff.toml is malformed."""


@dataclass(frozen=True)
class Entry:
    path: str
    codes: frozenset[str]
    task: str


def _entry(line: str, block: str | None, where: str) -> Entry:
    match = ENTRY_RE.fullmatch(line)
    if match is None:
        why = "entry without a task id" if "#" not in line else 'not \'"<file>" = ["<code>", ...]  # T<id>\''
        raise BaselineError(f"{where}: {why}: {line!r}")
    if block is None or match["id"] != block:
        raise BaselineError(f"{where}: entry id {match['id']} outside its '# {match['id']}' block")
    codes = frozenset(CODE_RE.findall(match["codes"]))
    if not codes:
        raise BaselineError(f"{where}: entry lists no rule code")
    return Entry(match["path"], codes, match["id"])


def parse_baseline(text: str) -> list[Entry]:
    lines = text.splitlines()
    if lines.count(BEGIN) != 1 or lines.count(END) != 1 or lines.index(BEGIN) > lines.index(END):
        raise BaselineError(f"ruff.toml needs exactly one '{BEGIN}' ... '{END}' region")
    start, stop = lines.index(BEGIN), lines.index(END)
    entries: list[Entry] = []
    block: str | None = None
    for number, line in enumerate(lines[start + 1 : stop], start + 2):
        header = HEADER_RE.fullmatch(line)
        if not line.strip():
            block = None
        elif header is not None:
            block = header["id"]
        else:
            entries.append(_entry(line, block, f"ruff.toml:{number}"))
    return entries


def stale(entries: Iterable[Entry], violations: set[tuple[str, str]]) -> list[str]:
    return [
        f"{e.path}: {code} # {e.task}" for e in entries for code in sorted(e.codes) if (e.path, code) not in violations
    ]


def uncovered(entries: Iterable[Entry], violations: set[tuple[str, str]]) -> list[str]:
    allowed = {(e.path, code) for e in entries for code in e.codes}
    return [f"{path}: {code}" for path, code in sorted(violations - allowed)]


def ruff(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "ruff", *args[:1], *GATE_CONFIG, *args[1:]]
    return subprocess.run(cmd, cwd=ROOT, input=stdin, capture_output=True, text=True, check=False)


def lint_stdin(source: str, filename: str = CORE_FILE) -> subprocess.CompletedProcess[str]:
    return ruff("check", "--no-cache", "--output-format", "concise", "--stdin-filename", filename, "-", stdin=source)


@pytest.fixture(scope="module")
def entries() -> list[Entry]:
    return parse_baseline(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def violations() -> set[tuple[str, str]]:
    """Every (file, code) in the tree with only the test policy applied (baseline switched off)."""
    policy = "lint.per-file-ignores = " + json.dumps(TEST_POLICY).replace(":", " =")
    done = ruff("check", "--no-cache", "--exit-zero", "--output-format", "json", "--config", policy, *LINT_DIRS)
    assert done.returncode == 0, done.stderr
    return {(str(Path(v["filename"]).relative_to(ROOT)), v["code"]) for v in json.loads(done.stdout)}


# --- the real tree -----------------------------------------------------------------------------------------------


def test_rule_set_and_limits_are_pinned() -> None:
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    assert {key: config[key] for key in config if key != "lint"} == TOP_LEVEL
    lint = config["lint"]
    assert set(lint) == LINT_KEYS, "no ignore/extend-ignore/extend-select: change the pinned set with ENGINEERING.md"
    assert lint["select"] == SELECT
    assert lint["explicit-preview-rules"] is True
    assert {key: lint[key] for key in LIMITS} == LIMITS


def test_policy_outside_the_baseline_is_only_the_test_policy(entries: list[Entry]) -> None:
    table = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["lint"]["per-file-ignores"]
    assert {path: frozenset(codes) for path, codes in table.items() if path not in TEST_POLICY} == {
        e.path: e.codes for e in entries
    }
    assert table["**/tests/**"] == TEST_POLICY["**/tests/**"]


def test_baseline_entries_are_not_stale(entries: list[Entry], violations: set[tuple[str, str]]) -> None:
    assert violations, "ruff reported nothing with the baseline off: the policy override did not apply"
    assert stale(entries, violations) == [], "fixed: delete these entries from the ruff.toml baseline"


def test_every_violation_is_baselined(entries: list[Entry], violations: set[tuple[str, str]]) -> None:
    assert uncovered(entries, violations) == []


def test_tree_passes_lint_and_format() -> None:
    for args in (("check", "--no-cache"), ("format", "--no-cache", "--check")):
        done = ruff(*args, *LINT_DIRS)
        assert done.returncode == 0, done.stdout + done.stderr


# --- baseline parser ---------------------------------------------------------------------------------------------


def _region(*body: str) -> str:
    return "\n".join(("[lint.per-file-ignores]", BEGIN, "", *body, "", END, ""))


def test_parser_reads_blocks_and_sub_ids() -> None:
    text = _region("# T1.06a", '"a.py" = ["C901", "S101"]  # T1.06a', "", "# T1.110", '"b.py" = ["E501"]  # T1.110')
    assert parse_baseline(text) == [
        Entry("a.py", frozenset({"C901", "S101"}), "T1.06a"),
        Entry("b.py", frozenset({"E501"}), "T1.110"),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (("# T1.110", '"a.py" = ["C901"]'), "entry without a task id"),
        (("# T1.110", '"a.py" = ["C901"]  # fix later'), "not '"),
        (("# T1.110", '"a.py" = ["C901"]  # T1.111'), "outside its '# T1.111' block"),
        (('"a.py" = ["C901"]  # T1.110',), "outside its '# T1.110' block"),
        (("# T1.110", '"**/x/*.py" = ["C901"]  # T1.110'), "not '"),
        (("# T1.110", '"a.py" = []  # T1.110'), "lists no rule code"),
    ],
)
def test_parser_rejects(body: tuple[str, ...], message: str) -> None:
    with pytest.raises(BaselineError, match=re.escape(message)):
        parse_baseline(_region(*body))


def test_parser_needs_the_markers() -> None:
    with pytest.raises(BaselineError, match="region"):
        parse_baseline('[lint.per-file-ignores]\n"a.py" = ["C901"]  # T1.110\n')


def test_stale_and_uncovered() -> None:
    baseline = [Entry("a.py", frozenset({"C901", "S101"}), "T1.110")]
    assert stale(baseline, {("a.py", "C901")}) == ["a.py: S101 # T1.110"]
    assert uncovered(baseline, {("a.py", "C901"), ("b.py", "E501")}) == ["b.py: E501"]


# --- planted violations (stdin, never written to the tree) ---------------------------------------------------------


def _branches(count: int) -> str:
    return "def f(a):\n" + "".join(f"    if a == {i}:\n        return {i}\n" for i in range(count)) + "    return -1\n"


def _nest(depth: int) -> str:
    lines = ["def f(xs):"]
    for level, head in enumerate(("for x in xs:", "if x:", "while x:", "if x > 1:")[:depth], 1):
        lines.append("    " * level + head)
    lines.append("    " * (depth + 1) + "xs.append(1)")
    lines.append("    return xs")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (_branches(10), "C901"),  # complexity 11
        ("def f(a, b, c, d, e):\n    return a + b + c + d + e\n", "PLR0913"),
        ("def f(a, b, c, d, e):\n    return a + b + c + d + e\n", "PLR0917"),
        (_nest(4), "PLR1702"),
    ],
)
def test_planted_violation_fails_with_rule_id(source: str, code: str) -> None:
    done = lint_stdin(source)
    assert done.returncode == 1
    assert f"{CORE_FILE}:" in done.stdout and f" {code} " in done.stdout, done.stdout


@pytest.mark.parametrize(
    "source",
    [_branches(9), "def f(a, b, c, d):\n    return a + b + c + d\n", _nest(3)],
    ids=["complexity-10", "4-parameters", "3-deep"],
)
def test_limit_itself_passes(source: str) -> None:
    done = lint_stdin(source)
    assert done.returncode == 0, done.stdout


def test_tests_may_take_more_parameters_but_not_more_complexity() -> None:
    tests_file = "hermes-plugin/tests/test_planted.py"
    assert lint_stdin("def test_f(a, b, c, d, e):\n    assert a\n", tests_file).returncode == 0
    assert " C901 " in lint_stdin(_branches(10), tests_file).stdout


def test_generated_code_is_excluded() -> None:
    done = lint_stdin(_branches(10), "hermes-plugin/glasses/generated/planted.py")
    assert done.returncode == 0 and "C901" not in done.stdout, done.stdout


def test_unformatted_code_fails_the_format_check() -> None:
    done = ruff("format", "--no-cache", "--check", "--stdin-filename", CORE_FILE, "-", stdin="x = ( 1 )\n")
    assert done.returncode == 1
