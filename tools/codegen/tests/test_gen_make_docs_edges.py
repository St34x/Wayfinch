# SPDX-License-Identifier: Apache-2.0
"""gen_make_docs.py edge cases: unusual `#:` lines and rules, gate-table cell forms, doc command forms, unreadable
inputs, what the generator writes (only its own file), and the make target failing closed without its tests."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import gen_make_docs as gen
import make_docs_model as model
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools/codegen/gen_make_docs.py"

MAKEFILE = """\
TEST_TARGETS :=
-include $(sort $(wildcard mk/*.mk))
.PHONY: help test

#: help — list the targets
help:
\t@echo help

#: test — run every test target
test: $(TEST_TARGETS)
\t@echo ok
"""
FRAGMENT = """\
# SPDX-License-Identifier: Apache-2.0
.PHONY: test-alpha
TEST_TARGETS += test-alpha

#: test-alpha — alpha unit tests
test-alpha:
\t@true
"""
GATES = """\
# Engineering

## 10. Gates

| Rule | Make target | State |
|---|---|---|
| Alpha works | `test-alpha` | live |
| Beta later | `check-beta` | planned |

## 11. Other
"""


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT), "--root", str(root), *args]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _git_add(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "mk").mkdir(parents=True)
    (root / "Makefile").write_text(MAKEFILE, encoding="utf-8")
    (root / "mk/alpha.mk").write_text(FRAGMENT, encoding="utf-8")
    (root / "ENGINEERING.md").write_text(GATES, encoding="utf-8")
    (root / "README.md").write_text("# Demo\n\nRun `make test`.\n", encoding="utf-8")
    assert _run(root).returncode == 0
    _git_add(root)
    assert _run(root, "--check").returncode == 0
    return root


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), encoding="utf-8")


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return _run(root, "--check")


def _names(text: str) -> list[str]:
    return [name for _line, name in model.commands(text)]


# --- `#:` lines --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "#: test-alpha\n",  # no description
        "#: test-alpha —\n",  # dash, no description
        "#: test-alpha - alpha unit tests\n",  # hyphen instead of the em dash
        "#: test-alpha – alpha unit tests\n",  # en dash
        "#:test-alpha — alpha unit tests\n",  # no space after the marker
    ],
)
def test_malformed_description_line_fails(tree: Path, line: str) -> None:
    _edit(tree / "mk/alpha.mk", "#: test-alpha — alpha unit tests\n", line)
    done = _check(tree)
    assert done.returncode == 1
    assert "mk/alpha.mk:5: malformed `#:` line" in done.stderr


@pytest.mark.parametrize("between", ["\n", "# a plain comment\n", ".PHONY: test-alpha\n"])
def test_description_line_not_directly_above_its_rule_fails(tree: Path, between: str) -> None:
    _edit(tree / "mk/alpha.mk", "alpha unit tests\ntest-alpha:", f"alpha unit tests\n{between}test-alpha:")
    done = _check(tree)
    assert done.returncode == 1
    assert "mk/alpha.mk:5: `#: test-alpha` is not directly above a rule of `test-alpha`" in done.stderr
    assert "target `test-alpha` has no `#:` line" in done.stderr


def test_description_line_naming_another_target_fails(tree: Path) -> None:
    _edit(tree / "mk/alpha.mk", "#: test-alpha —", "#: test-beta —")
    done = _check(tree)
    assert done.returncode == 1
    assert "`#: test-beta` is not directly above a rule of `test-beta`" in done.stderr


@pytest.mark.parametrize(
    ("rule", "name"),
    [("%.out: %.in", "%.out"), ("$(GEN):", "$(GEN)"), ("${GEN}::", "${GEN}"), ("a b &: c", "&")],
)
def test_rule_without_literal_name_fails(tree: Path, rule: str, name: str) -> None:
    _append(tree / "mk/alpha.mk", f"\n{rule}\n\t@true\n")
    done = _check(tree)
    assert done.returncode == 1
    assert f"rule target `{name}` is not a literal name" in done.stderr


def test_multiple_targets_on_one_rule_each_need_a_line(tree: Path) -> None:
    _append(tree / "mk/alpha.mk", "\n#: one — first\none two:\n\t@true\n")
    done = _check(tree)
    assert done.returncode == 1
    assert "target `two` has no `#:` line" in done.stderr
    assert "target `one`" not in done.stderr


def test_phony_only_name_is_not_a_target(tree: Path) -> None:
    _edit(tree / "mk/alpha.mk", ".PHONY: test-alpha", ".PHONY: test-alpha ghost")
    assert _check(tree).returncode == 0
    assert "`ghost`" not in (tree / gen.OUTPUT).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "line",
    ["URL := http://example.com/x", "X ?= a:b", "Y += c:d", "export Z := e:f", "vpath %.c src:lib", "override Q = 1"],
)
def test_assignments_and_directives_with_colons_are_not_rules(line: str) -> None:
    assert model.rule_targets(line) is None


# --- `make test` entries -----------------------------------------------------------------------------------------


def test_test_target_added_in_a_conditional_needs_a_live_row(tree: Path) -> None:
    block = "\nifeq ($(CI),1)\nTEST_TARGETS += test-gamma\nendif\n\n#: test-gamma — g\ntest-gamma:\n"
    _append(tree / "mk/alpha.mk", block)
    assert _run(tree).returncode == 0
    done = _check(tree)
    assert done.returncode == 1
    assert "`make test` runs `test-gamma`, but no live row" in done.stderr


@pytest.mark.parametrize("assignment", ["  TEST_TARGETS += test-gamma", "override TEST_TARGETS += test-gamma"])
def test_test_target_added_by_indented_or_override_line_needs_a_live_row(tree: Path, assignment: str) -> None:
    _append(tree / "mk/alpha.mk", f"\nifeq ($(CI),1)\n{assignment}\nendif\n\n#: test-gamma — g\ntest-gamma:\n")
    assert _run(tree).returncode == 0
    assert _check(tree).returncode == 1


def test_non_literal_test_target_entry_fails(tree: Path) -> None:
    _edit(tree / "mk/alpha.mk", "TEST_TARGETS += test-alpha", "TEST_TARGETS += test-alpha $(EXTRA)")
    done = _check(tree)
    assert done.returncode == 1
    assert "TEST_TARGETS entry `$(EXTRA)` is not a literal target name" in done.stderr


# --- section 10 gate table ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["Live", "LIVE", "live-ish", "", "done"])
def test_state_cell_must_start_with_live_or_planned(tree: Path, state: str) -> None:
    _edit(tree / "ENGINEERING.md", "`test-alpha` | live |", f"`test-alpha` | {state} |")
    done = _check(tree)
    assert done.returncode == 1
    assert "ENGINEERING.md:7: State must start with `live` or `planned`" in done.stderr


def test_state_with_words_after_live_counts_as_live(tree: Path) -> None:
    _edit(tree / "ENGINEERING.md", "`test-alpha` | live |", "`test-alpha` | live at merge time |")
    assert _check(tree).returncode == 0


def test_row_with_two_targets_checks_each(tree: Path) -> None:
    _edit(tree / "ENGINEERING.md", "| `test-alpha` | live", "| `test-alpha`, `test-delta` | live")
    done = _check(tree)
    assert done.returncode == 1
    assert "ENGINEERING.md:7: live row names `test-delta`, which no rule defines" in done.stderr


def test_escaped_pipe_stays_inside_its_cell(tree: Path) -> None:
    _edit(tree / "ENGINEERING.md", "| Alpha works |", "| Alpha a \\| b works |")
    assert _check(tree).returncode == 0


def test_row_with_a_wrong_cell_count_fails(tree: Path) -> None:
    _edit(tree / "ENGINEERING.md", "| Alpha works |", "| Alpha | works |")
    done = _check(tree)
    assert done.returncode == 1
    assert "ENGINEERING.md:7: row has 4 cells, the header 3" in done.stderr


def test_planned_row_naming_a_defined_target_is_allowed(tree: Path) -> None:
    _edit(tree / "ENGINEERING.md", "`check-beta` | planned", "`test-alpha` | planned")
    assert _check(tree).returncode == 0


def test_section_10_missing_cannot_be_checked(tree: Path) -> None:
    _edit(tree / "ENGINEERING.md", "## 10. Gates", "## 9. Gates")
    done = _check(tree)
    assert done.returncode == 2
    assert "no `## 10.` section" in done.stderr


# --- doc commands ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("```sh\n$ make bogus\n```\n", ["bogus"]),
        ("```console\n$ VAR=1 make bogus\n```\n", ["bogus"]),
        ("```sh\nmake -C sub bogus\n```\n", ["bogus"]),
        ("```sh\nmake -j 4 -f x.mk a b\n```\n", ["a", "b"]),
        ("```sh\nmake --directory=sub bogus\n```\n", ["bogus"]),
        ("```sh\ncd x; make a || make b | tee log\n```\n", ["a", "b"]),
        ("```sh\nmake a > log 2>&1\n```\n", ["a"]),
        ("~~~~\n```\nmake a\n```\n~~~~\nmake b\n", ["a"]),
        ("Inline `make help` and `make  a\nb`.\n", ["help", "a", "b"]),
        ("Prose: make bogus work, `make` alone, `make <target>`.\n", []),
        ("    make indented-code-block\n", []),
    ],
)
def test_make_command_forms(text: str, names: list[str]) -> None:
    assert _names(text) == names


@pytest.mark.xfail(strict=True, reason="known gap: a make behind a command prefix (sudo, env, time) is not read")
@pytest.mark.parametrize("line", ["sudo make bogus", "env V=1 make bogus", "time make bogus"])
def test_make_behind_a_command_prefix_is_read(line: str) -> None:
    assert _names(f"```sh\n{line}\n```\n") == ["bogus"]


def test_doc_command_in_a_table_cell_fails(tree: Path) -> None:
    (tree / "docs").mkdir(exist_ok=True)
    (tree / "docs/use.md").write_text("| Step | Command |\n|---|---|\n| one | `make test-zeta` |\n", encoding="utf-8")
    done = _check(tree)
    assert done.returncode == 1
    assert "docs/use.md:3: `make test-zeta`: no rule defines it and no planned gate row" in done.stderr


def test_doc_naming_a_planned_target_is_allowed(tree: Path) -> None:
    _append(tree / "README.md", "\nLater: `make check-beta`.\n")
    assert _check(tree).returncode == 0


def test_private_team_docs_are_not_read_also_when_tracked(tree: Path) -> None:
    (tree / gen.PRIVATE).mkdir()
    (tree / gen.PRIVATE / "notes.md").write_text("```sh\n$ make private-only\n```\n", encoding="utf-8")
    assert _check(tree).returncode == 0


def test_unreadable_public_doc_cannot_be_checked(tree: Path) -> None:
    (tree / "bad.md").write_bytes(b"\xff\xfe not utf-8\n")
    done = _check(tree)
    assert done.returncode == 2
    assert "bad.md: cannot read" in done.stderr


# --- inputs and outputs ------------------------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
@pytest.mark.parametrize("name", ["Makefile", "mk/alpha.mk", "ENGINEERING.md"])
def test_unreadable_input_cannot_be_checked(tree: Path, name: str) -> None:
    (tree / name).chmod(0)
    try:
        done = _run(tree, "--check")
    finally:
        (tree / name).chmod(0o644)
    assert done.returncode == 2
    assert done.stderr.startswith("gen_make_docs: ")


def test_makefile_with_only_special_targets_cannot_be_generated(tree: Path) -> None:
    (tree / "Makefile").write_text(".PHONY: a\n.DEFAULT_GOAL := a\n", encoding="utf-8")
    (tree / "mk/alpha.mk").write_text("# SPDX-License-Identifier: Apache-2.0\n.PHONY: b\n", encoding="utf-8")
    done = _run(tree)
    assert done.returncode == 2
    assert "no target defined" in done.stderr


def test_stale_reference_names_the_regenerate_command(tree: Path) -> None:
    _edit(tree / gen.OUTPUT, "alpha unit tests", "alpha tests")
    done = _check(tree)
    assert done.returncode == 1
    assert f"is stale (edited, or a `#:` line changed); run: {gen.REGENERATE}" in done.stderr
    assert gen.REGENERATE == "python3 tools/codegen/gen_make_docs.py"


def test_generated_header_is_the_first_line_only(tree: Path) -> None:
    lines = (tree / gen.OUTPUT).read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("<!-- @generated by tools/codegen/gen_make_docs.py")
    assert [n for n, line in enumerate(lines) if "@generated" in line] == [0]


def test_generation_writes_only_its_own_file(tree: Path) -> None:
    out_dir = tree / gen.OUTPUT_DIR
    (out_dir / "config.md").write_text("hand edited\n", encoding="utf-8")
    (out_dir / "extra.md").write_text("extra\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in tree.rglob("*") if p.is_file() and ".git" not in p.parts}
    _edit(tree / "mk/alpha.mk", "alpha unit tests", "alpha unit tests v2")
    before[tree / "mk/alpha.mk"] = (tree / "mk/alpha.mk").read_bytes()
    assert _run(tree).returncode == 0
    after = {p: p.read_bytes() for p in tree.rglob("*") if p.is_file() and ".git" not in p.parts}
    changed = sorted(str(p.relative_to(tree)) for p in after if before.get(p) != after[p])
    assert changed == [gen.OUTPUT.as_posix()]


def test_generation_replaces_a_symlink_without_writing_through_it(tree: Path) -> None:
    out = tree / gen.OUTPUT
    victim = tree / "victim.md"
    victim.write_text("keep me\n", encoding="utf-8")
    out.unlink()
    out.symlink_to(victim)
    assert _run(tree).returncode == 0
    assert not out.is_symlink()
    assert victim.read_text(encoding="utf-8") == "keep me\n"


# --- the make target ---------------------------------------------------------------------------------------------


def test_make_target_fails_closed_without_its_tests(tmp_path: Path) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make not installed")
    shutil.copy(ROOT / "mk/make-docs.mk", tmp_path / "make-docs.mk")
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "PY"}}
    argv = [make, "-f", "make-docs.mk", f"PY={sys.executable}", "check-make-docs"]
    done = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert "check-make-docs: no tools/codegen/tests/test_gen_make_docs*.py" in done.stderr
