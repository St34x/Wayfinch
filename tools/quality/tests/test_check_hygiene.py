# SPDX-License-Identifier: Apache-2.0
"""Unit tests of tools/quality/check_hygiene.py (T1.102): TODO ids, commit subjects, gate start, glossary.

CLI tests on scratch repositories and the real tree: test_check_hygiene_cli.py.

The module is loaded by path (tools/quality is not a package). Planted keywords are assembled at run time
(`KW_TODO`, ...) so this file itself never holds a bare marker that the gate would flag.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_hygiene.py"
KW_TODO = "TO" + "DO"
KW_FIXME = "FIX" + "ME"
KW_XXX = "X" + "XX"
ROUND = "rou" + "nd"
GOOD_GLOSSARY = (
    "# Glossary\n\n| Term | Meaning | Not to be called |\n|---|---|---|\n"
    "| **app** | The phone app | client |\n| **lane** | A channel \\| routed | topic |\n"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_hygiene", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hy = _load()


# --- (a) task-id markers ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("a.py", f"# {KW_TODO} fix"),
        ("a.py", f"x = 1  # {KW_TODO}: later"),
        ("a.py", f"# note, {KW_TODO} fix this"),
        ("a.py", f"# {KW_FIXME}(T1.2): only the first keyword may carry an id"),
        ("a.py", f"# {KW_XXX} hack"),
        ("a.py", f"# {KW_TODO}(T1): id without minor"),
        ("a.py", f"# {KW_TODO}(T1.23 unclosed"),
        ("a.py", f"# {KW_TODO} (T1.23): space before the id"),
        ("run.sh", f"\t# {KW_TODO} fix"),
        ("Makefile", f"# {KW_TODO}"),
        ("A.kt", f"val x = 1 // {KW_XXX} hack"),
        ("A.kt", f"/* {KW_TODO} later */"),
        ("A.kt", f" * {KW_FIXME} in a block comment"),
        ("b.gradle", f"// {KW_TODO}"),
        ("README.md", f"<!-- {KW_TODO} write this -->"),
        ("res.xml", f"<!--{KW_FIXME}-->"),
        ("Dockerfile", f"# {KW_TODO} unknown extension still checked"),
    ],
)
def test_marker_without_id_fails(path: str, line: str) -> None:
    findings = hy.todo_findings(path, f"ok\n{line}\n")
    assert len(findings) == 1
    assert findings[0].startswith(f"{path}:2: ")


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("a.py", f"# {KW_TODO}(T1.23): split the parser"),
        ("a.py", f"x = 1  # {KW_TODO}(P.4): publish step"),
        ("run.sh", f"# {KW_TODO}(S05a): spike follow-up"),
        ("a.py", f"# {KW_TODO}(T1.06a): sub-id of an umbrella task"),
        ("A.kt", f"// {KW_TODO}(T1.105): generated types"),
        ("README.md", f"<!-- {KW_TODO}(T1.103): headings -->"),
    ],
)
def test_marker_with_id_passes(path: str, line: str) -> None:
    assert hy.todo_findings(path, line + "\n") == []


@pytest.mark.parametrize(
    ("path", "line"),
    [
        # The tree's known look-alikes (T1.102: no false positives).
        ("protocol/envelope.schema.json", f'            "{KW_TODO}",'),
        ("protocol/README.md", f"| `meeting.hint` | S→D | `kind` `TERM`\\|`{KW_TODO}`\\|`FALSE` |"),
        ("protocol/README.md", "escapes every non-ASCII character as `\\uXXXX`"),
        ("tools/tests/t.sh", 'WORK=$(mktemp -d "${TMPDIR:-/tmp}/test-check-spdx.XXXXXX") || exit 1'),
        ("protocol/tests/t.py", "N = 12  # astral code point escaped as a \\uXXXX\\uXXXX surrogate pair"),
        ("ENGINEERING.md", f"- **No `{KW_TODO}`/`{KW_FIXME}` without a task id**: `{KW_TODO}(T1.23): …`"),
        ("a.py", f'URL = "https://example.com/{KW_TODO}"'),
        ("a.py", f"# a list of {KW_TODO.lower()} items and {KW_TODO}S"),
        ("a.sh", f'echo "${{name#{KW_TODO}}}"'),
        ("docs/x.md", f"# {KW_TODO} heading in Markdown is not a comment"),
    ],
)
def test_look_alikes_pass(path: str, line: str) -> None:
    assert hy.todo_findings(path, line + "\n") == []


def test_multiline_html_comment_is_checked() -> None:
    text = f"intro\n<!--\n  {KW_TODO} fix\n-->\n"
    assert [f.split(" ")[0] for f in hy.todo_findings("docs/a.md", text)] == ["docs/a.md:3:"]


def test_two_markers_on_one_line_both_reported() -> None:
    assert len(hy.todo_findings("a.py", f"# {KW_TODO} and {KW_FIXME}\n")) == 2


# --- (b) commit subjects ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "T1.102: hygiene checker",
        "T12.3: x",
        "T1.06a: sub-id",
        "T1.15e: sub-id",
        "P.04: publish",
        "S05: spike",
        "S05a: spike rerun",
        "merge: T1.98 size checker",
        "design: x",
        "adr: x",
        "research: x",
        "spike: x",
        "mockups: x",
        "docs: x",
        "plan: x",
        "tasks: x",
        "chore: x",
        "qa: x",
        "review: x",
        "status: x",
        "night: x",
        "fix: x",
        "feat: x",
        "test: x",
        "refactor: x",
    ],
)
def test_good_subject(subject: str) -> None:
    assert hy.subject_ok(subject)


@pytest.mark.parametrize(
    "subject",
    [
        "fix stuff",
        "Merge branch 'task/T1.98-size-checker'",
        "T1.102 hygiene checker",
        "T1.102:no space",
        "T1.102: ",
        "T1: no minor",
        "QA: wrong case",
        "Review: wrong case",
        f"QA {ROUND} 2: x",
        "Plan: x",
        "brief: not a listed prefix",
        "T1.06ab: two letters",
        " docs: leading space",
    ],
)
def test_bad_subject(subject: str) -> None:
    assert not hy.subject_ok(subject)


# --- (b) gate start file and range syntax -----------------------------------------------------------------------

HASH = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("# only comments\n\n", None),
        (f"{HASH}\n", HASH),
        (f"# header\n  {HASH}  \n", HASH),
        ("a" * 64 + "\n", "a" * 64),
    ],
)
def test_parse_gate_start(text: str, expected: str | None) -> None:
    assert hy.parse_gate_start(text) == expected


@pytest.mark.parametrize("text", ["HEAD\n", HASH[:12], HASH.upper(), f"{HASH}\n{HASH}\n", f"{HASH} trailing"])
def test_parse_gate_start_rejects_invalid(text: str) -> None:
    with pytest.raises(hy.HygieneError):
        hy.parse_gate_start(text)


@pytest.mark.parametrize("rng", ["", "-n1", "..HEAD", "HEAD..", "..", "...HEAD", "HEAD...", "...", " ..HEAD"])
def test_range_syntax_fails_closed(rng: str) -> None:
    with pytest.raises(hy.HygieneError):
        hy.check_range_syntax(rng)


@pytest.mark.parametrize("rng", ["HEAD~2..HEAD", "base...main", "origin/main..HEAD", "HEAD", "^HEAD"])
def test_range_syntax_accepts(rng: str) -> None:
    hy.check_range_syntax(rng)


# --- (c) glossary ------------------------------------------------------------------------------------------------


def test_glossary_clean() -> None:
    assert hy.glossary_findings(GOOD_GLOSSARY) == []


def test_glossary_duplicate_term_fails() -> None:
    text = GOOD_GLOSSARY + "| **App** | Something else entirely | x |\n"
    findings = hy.glossary_findings(text)
    assert len(findings) == 1
    assert "docs/glossary.md:7:" in findings[0] and "'app'" in findings[0] and "line 5" in findings[0]


def test_glossary_duplicate_definition_fails() -> None:
    text = GOOD_GLOSSARY + "| **phone app** | the  phone app. | x |\n"
    findings = hy.glossary_findings(text)
    assert len(findings) == 1
    assert "docs/glossary.md:7:" in findings[0] and "same definition" in findings[0]


def test_glossary_without_rows_fails() -> None:
    assert hy.glossary_findings("# Glossary\n\nno table\n") != []
