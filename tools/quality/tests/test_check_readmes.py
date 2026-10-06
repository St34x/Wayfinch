# SPDX-License-Identifier: Apache-2.0
"""Unit tests of tools/quality/check_readmes.py (T1.103): module-list parsing and README structure rules."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_readmes.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_readmes_unit_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = _load()
GOOD = (
    "<!-- SPDX-License-Identifier: Apache-2.0 -->\n# Title\n\n"
    "## Purpose\n\nWhat it is for.\n\n## Public API\n\n- `f()`\n\n## Invariants\n\n- never X\n"
)
REF = "ENGINEERING.md §1"
OVERRIDE = f"protocol | length-override | reason: protocol specification | ref: {REF}"


def _padded(total: int) -> str:
    """GOOD grown to exactly `total` lines with Invariants bullets."""
    base = GOOD.splitlines()
    return "\n".join(base + [f"- rule {i}" for i in range(total - len(base))]) + "\n"


def _spec(head: str = GOOD) -> str:
    return head + "\n## Envelope\n\n" + "\n".join(f"| row {i} |" for i in range(50)) + "\n"


# --- module list -----------------------------------------------------------------------------------------------------


def test_parse_forms() -> None:
    entries = cr.parse_modules(f"# comment\n\nmk\n{OVERRIDE}\ntools/test-hermes | part-of: tools\ntools\n")
    assert [(e.path, e.part_of, e.ref) for e in entries] == [
        ("mk", None, None),
        ("protocol", None, REF),
        ("tools/test-hermes", "tools", None),
        ("tools", None, None),
    ]


@pytest.mark.parametrize(
    "line",
    [
        f"protocol | length-override | ref: {REF}",  # no reason
        "protocol | length-override | reason: protocol specification",  # no ref
        f"protocol | length-override | reason: | ref: {REF}",  # empty reason
        f"protocol | length-override | reason: too long to fit | ref: {REF}",  # not a specification
        "protocol | length-override | reason: protocol specification | ref:",  # empty ref
        "protocol | length-override | reason: protocol specification | ref: ENGINEERING.md",  # no section
        "protocol | length-override | reason: protocol specification | ref: ENGINEERING.md §",
        "protocol | length-override | reason: protocol specification | ref: ENGINEERING.md 1",
        "protocol | length-override | reason: protocol specification | ref: see ENGINEERING.md §1 maybe",
        "protocol | length-override | reason: protocol specification | ref: /abs/doc.md §1",
        "protocol | length-override | reason: protocol specification | ref: ../doc.md §1",
        "protocol | length-override | reason: protocol specification | ref: .hidden/doc.md §1",  # dot-directory
        "protocol | length-override | reason: protocol specification | ref: docs/.notes/x.md §1",
        "protocol | length-override | protocol specification | ENGINEERING.md §1",  # field names missing
        f"protocol | reason: protocol specification | ref: {REF}",  # override keyword missing
        f"protocol | override | reason: protocol specification | ref: {REF}",
        f"protocol | length-override | reason: protocol specification | ref: {REF} | extra",
        "tools/x | part-of:",
        "tools/x | part of: tools",
        "../outside",
        "/abs/path",
        "Tools/Upper",
        "tools/",
        "tools//x",
        "./tools",
    ],
)
def test_bad_lines_fail_closed(line: str) -> None:
    with pytest.raises(cr.ReadmeError):
        cr.parse_modules(f"mk\n{line}\n")


@pytest.mark.parametrize("text", ["", "# only comments\n\n", "   \n"])
def test_empty_list_fails_closed(text: str) -> None:
    with pytest.raises(cr.ReadmeError, match="no modules"):
        cr.parse_modules(text)


def test_duplicate_fails_closed() -> None:
    with pytest.raises(cr.ReadmeError, match="twice"):
        cr.parse_modules(f"mk\nprotocol\n{OVERRIDE}\n")


# --- README structure ------------------------------------------------------------------------------------------------


def test_good_readme_passes() -> None:
    assert cr.check_readme(GOOD, spec=False) == []


def test_forty_lines_pass_fortyone_fail() -> None:
    assert cr.check_readme(_padded(40), spec=False) == []
    problems = cr.check_readme(_padded(41), spec=False)
    assert problems == ["41 lines, limit 40 (a spec document needs a length-override, ENGINEERING.md §1)"]


@pytest.mark.parametrize("missing", ["## Purpose\n", "## Public API\n", "## Invariants\n"])
def test_missing_heading_fails(missing: str) -> None:
    problems = cr.check_readme(GOOD.replace(missing, "### Gone\n"), spec=False)
    assert any("first `##` sections must be exactly" in p for p in problems), problems


@pytest.mark.parametrize(
    "old,new",
    [
        ("## Purpose\n", "## Purpose:\n"),
        ("## Public API\n", "## Public api\n"),
        ("## Invariants\n", "## Invariants ##\n"),
        ("## Public API\n", "#### Public API\n"),
    ],
)
def test_heading_names_are_exact(old: str, new: str) -> None:
    assert cr.check_readme(GOOD.replace(old, new), spec=False) != []


def test_wrong_order_fails() -> None:
    swapped = GOOD.replace("## Public API", "## TMP").replace("## Invariants", "## Public API")
    assert cr.check_readme(swapped.replace("## TMP", "## Invariants"), spec=False) != []


def test_prose_before_purpose_fails() -> None:
    problems = cr.check_readme(GOOD.replace("# Title\n", "# Title\n\nIntro text.\n"), spec=False)
    assert any("may precede `## Purpose`" in p for p in problems), problems


@pytest.mark.parametrize("extra", ["# Second title\n", "### Sub\n", "```\ncode\n```\n", "| a | b |\n"])
def test_only_title_and_comments_before_purpose(extra: str) -> None:
    assert cr.check_readme(GOOD.replace("\n## Purpose", f"\n{extra}\n## Purpose"), spec=False) != []


def test_no_title_is_fine() -> None:
    assert cr.check_readme(GOOD.replace("# Title\n", ""), spec=False) == []


@pytest.mark.parametrize("section", ["Purpose", "Public API", "Invariants"])
def test_empty_section_fails(section: str) -> None:
    lines = GOOD.splitlines()
    start = lines.index(f"## {section}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    text = "\n".join(lines[: start + 1] + ["", "<!-- comment only -->", ""] + lines[end:]) + "\n"
    assert any(f"`## {section}` is empty" in p for p in cr.check_readme(text, spec=False))


def test_extra_section_fails_without_override() -> None:
    problems = cr.check_readme(GOOD + "\n## Usage\n\nmore\n", spec=False)
    assert any("extra section `## Usage`" in p for p in problems), problems


def test_heading_inside_fence_does_not_count() -> None:
    fenced = GOOD + "\n```make\n# SPDX-License-Identifier: Apache-2.0\n## Not a section\n```\n"
    assert cr.check_readme(fenced, spec=False) == []
    tilde = GOOD.replace("- never X", "~~~\n## Purpose\n~~~")
    assert cr.check_readme(tilde, spec=False) == []


def test_unclosed_fence_hides_nothing_before_it() -> None:
    text = "```\n## Purpose\n\nx\n\n## Public API\n\ny\n\n## Invariants\n\nz\n"
    assert cr.check_readme(text, spec=False) != []


def test_setext_heading_fails() -> None:
    problems = cr.check_readme(GOOD + "\nUsage\n-----\n\nmore\n", spec=False)
    assert any("setext" in p for p in problems), problems
    assert cr.check_readme(GOOD.replace("Invariants\n\n- never X", "Invariants\n\nx\n=="), spec=False) != []


def test_thematic_break_and_tables_are_text() -> None:
    assert cr.check_readme(GOOD + "\n---\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", spec=False) == []


# --- spec documents (length-override) --------------------------------------------------------------------------------


def test_spec_passes_only_with_override() -> None:
    assert cr.check_readme(_spec(), spec=True) == []
    problems = cr.check_readme(_spec(), spec=False)
    assert any("limit 40" in p for p in problems) and any("extra section `## Envelope`" in p for p in problems)


def test_spec_headings_must_come_first() -> None:
    moved = "# Title\n\n## Envelope\n\nrows\n\n" + GOOD.split("# Title\n\n", 1)[1] + "\n".join(["x"] * 50) + "\n"
    problems = cr.check_readme(moved, spec=True)
    assert any("first `##` sections must be exactly" in p for p in problems), problems


@pytest.mark.parametrize("missing", ["## Purpose\n", "## Public API\n", "## Invariants\n"])
def test_spec_missing_heading_fails(missing: str) -> None:
    assert cr.check_readme(_spec(GOOD.replace(missing, "")), spec=True) != []


def test_stale_override_fails() -> None:
    problems = cr.check_readme(GOOD, spec=True)
    assert any("length-override is not needed" in p for p in problems), problems
