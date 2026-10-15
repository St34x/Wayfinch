# SPDX-License-Identifier: Apache-2.0
"""HTML comment lines in the module README gate: a line counts as a comment only when it holds nothing but whole
comments, each closed cleanly at its first `-->`, so no text hides between comments. Anything a browser or GitHub
reads differently (`--!>` closers, abrupt `<!-->` / `<!--->`) is text: the gate fails closed.
"""

import importlib.util
import re
import time
from pathlib import Path
from types import ModuleType

import pytest

TOP = Path(__file__).resolve().parents[3]
SCRIPT = TOP / "tools/quality/check_readmes.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_readmes_comments_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = _load()
HEAD = "<!-- SPDX-License-Identifier: Apache-2.0 -->\n# Title\n\n"
BODY = "## Purpose\n\n- does X\n\n## Public API\n\n- `f()`\n\n## Invariants\n\n- never Y\n"
BEFORE_PURPOSE = "only a `# ` title, blank lines and comments may precede `## Purpose`"


@pytest.mark.parametrize(
    "line",
    [
        "<!-- note -->",
        "  <!-- note -->\t",
        "<!---->",
        "<!-- a --> <!-- b -->",
        "<!-- <SCRIPT>x</script > -->",
        "<!---x-->",
        "<!--!>x-->",
        "<!-- a --!-->",
    ],
)
def test_whole_comment_lines_are_comments(line: str) -> None:
    assert cr.is_comment_line(line)
    assert cr.classify([line])[0].kind == "comment"


@pytest.mark.parametrize(
    "line",
    [
        "<!-- not closed",
        "<!-->",
        "<!--->",
        "<!-->hidden-->",
        "<!--->hidden-->",
        "<!-->-->",
        "<!-- a -->  <!-->hidden-->",
        "<!-- a -- >",
        "<!-- note --!>",
        "<!-- a --!><!-- b -->",
        "<!-- a --> <!-- b --!>",
        "<!-- a --> hidden <!-- b -->",
        "<!-- a --!> shown by a browser -->",
        "<!-- a --> tail",
        "lead <!-- a -->",
        "\u00a0<!-- a -->",
        "<!- a ->",
    ],
)
def test_text_outside_a_comment_is_not_a_comment(line: str) -> None:
    assert not cr.is_comment_line(line)
    assert cr.classify([line])[0].kind == "text"


@pytest.mark.parametrize("line", ["<!-- note --!>", "<!-->hidden text-->", "<!--->hidden text-->"])
def test_comment_a_renderer_reads_differently_may_not_precede_purpose(line: str) -> None:
    problems = cr.check_readme(HEAD + line + "\n\n" + BODY, spec=False)
    assert problems == [f"line 4: {BEFORE_PURPOSE}"]


def test_text_between_comments_before_purpose_is_a_finding() -> None:
    problems = cr.check_readme(HEAD + "<!-- a --> hidden <!-- b -->\n\n" + BODY, spec=False)
    assert problems == [f"line 4: {BEFORE_PURPOSE}"]


@pytest.mark.parametrize(
    ("line", "empty"),
    [
        ("<!-- a -->", True),
        ("<!-- a --> <!-- b -->", True),
        ("<!-- a --!>", False),
        ("<!-->words-->", False),
        ("<!-- a --> words <!-- b -->", False),
    ],
)
def test_section_with_only_comments_is_empty(line: str, empty: bool) -> None:
    text = HEAD + BODY.replace("- does X", line)
    expected = ["line 4: `## Purpose` is empty"] if empty else []
    assert cr.check_readme(text, spec=False) == expected


@pytest.mark.parametrize("unit", ["<!---->", "<!-- -->", "<!--"])
def test_a_long_line_is_scanned_in_linear_time(unit: str) -> None:
    """A 400 KB line once took ~20 s (quadratic re-search); linear scanning takes milliseconds. 2 s keeps slow CI
    green while a quadratic scan still fails by far."""
    line = unit * (400_000 // len(unit))
    started = time.perf_counter()
    cr.is_comment_line(line)
    cr.is_comment_line(line + "-")
    assert time.perf_counter() - started < 2.0


def test_no_regex_of_the_gate_parses_html() -> None:
    patterns = [value.pattern for value in vars(cr).values() if isinstance(value, re.Pattern)]
    assert patterns
    assert not [pattern for pattern in patterns if "<!" in pattern]
