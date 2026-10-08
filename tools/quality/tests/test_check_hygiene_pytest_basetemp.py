# SPDX-License-Identifier: Apache-2.0
"""Tests that every pytest run of `make test` keeps its temp files under `.local/` (T1.153, GHSA-6w46-j5rx-g56g).

Without `--basetemp` pytest writes `tmp_path` data (scratch Hermes homes, planted fake keys) to the shared, predictable
`/tmp/pytest-of-<user>` base dir. Every pytest recipe line of `Makefile` and `mk/*.mk` must pass
`--basetemp=.local/pytest-tmp/<dir>` with one plain, unique `<dir>` per call: pytest empties its basetemp at the start
of a run, so a shared dir, the parent itself or a dir nested in another call's dir lets one call wipe another's files.
Any other non-comment line that names pytest (a variable, a space-indented continuation, a target line) is a finding,
so an unusual spelling cannot hide a call from the check. Pytest subprocesses that tests start get a `--basetemp` too.
"""

import re
from pathlib import Path

import pytest

TOP = Path(__file__).resolve().parents[3]
# The word pytest or py.test, also as `-mpytest` and `bin/pytest`; case-sensitive, so `$(PYTEST)` is not a mention.
PYTEST_WORD = re.compile(r"(?:\b|(?<=-m))py\.?test\b")
BASETEMP = re.compile(r"--basetemp=(\S*)")
# The `mkdir -p .local/pytest-tmp` parent token, which names the shared dir, not a pytest call.
TMP_PARENT = re.compile(r"(?<!\S)\.local/pytest-tmp(?!\S)")
# One plain dir right under the parent: not the parent itself, not nested, no `.`/`..`, no make variables.
PLAIN_DIR = re.compile(r"\.local/pytest-tmp/[A-Za-z0-9_-]+")
# Built at run time so this file does not match its own subprocess scan.
SUBPROCESS_ARGV = '"-m", ' + '"pytest"'


def pytest_calls(line: str) -> int:
    """How often a line names pytest, not counting `--basetemp=` values and the `.local/pytest-tmp` parent."""
    return len(PYTEST_WORD.findall(TMP_PARENT.sub("", BASETEMP.sub("", line))))


def mention_lines(texts: dict[str, str]) -> list[tuple[str, str]]:
    """(file:line, text) of every line that names pytest, except `#` comment lines (including `#:` doc lines)."""
    return [
        (f"{name}:{number}", line)
        for name, text in texts.items()
        for number, line in enumerate(text.splitlines(), 1)
        if not line.startswith("#") and pytest_calls(line)
    ]


def recipe_lines(texts: dict[str, str]) -> list[tuple[str, str]]:
    """The checked lines: every recipe line (tab-indented) that names pytest."""
    return [(where, line) for where, line in mention_lines(texts) if line.startswith("\t")]


def dir_problems(where: str, dirs: list[str], seen: dict[str, str]) -> list[str]:
    """Basetemp values that are not one plain dir under `.local/pytest-tmp/` or that an earlier call already uses."""
    problems: list[str] = []
    for found in dirs:
        if not PLAIN_DIR.fullmatch(found):
            problems.append(f"{where}: --basetemp={found} is not one plain dir .local/pytest-tmp/<name>")
        elif found in seen:
            problems.append(f"{where}: --basetemp={found} already used at {seen[found]}")
        else:
            seen[found] = where
    return problems


def basetemp_problems(texts: dict[str, str]) -> list[str]:
    """Pytest mentions outside a checked recipe line, then recipe lines without one plain, unique basetemp per call."""
    problems = [
        f"{where}: pytest mentioned outside a checked recipe line"
        for where, line in mention_lines(texts)
        if not line.startswith("\t")
    ]
    seen: dict[str, str] = {}
    for where, line in recipe_lines(texts):
        calls, dirs = pytest_calls(line), BASETEMP.findall(line)
        if len(dirs) != calls:
            problems.append(f"{where}: {calls} pytest call(s), {len(dirs)} --basetemp")
        problems += dir_problems(where, dirs, seen)
    return problems


def real_recipes() -> dict[str, str]:
    paths = [TOP / "Makefile", *sorted((TOP / "mk").glob("*.mk"))]
    return {str(path.relative_to(TOP)): path.read_text(encoding="utf-8") for path in paths}


def demo(recipe: str) -> dict[str, str]:
    return {"mk/zz-demo.mk": f"zz-demo:\n\t{recipe}\n"}


def test_real_tree_pytest_recipes_use_unique_basetemp_under_local() -> None:
    texts = real_recipes()
    checked = {where.rsplit(":", 1)[0] for where, _ in recipe_lines(texts)}
    invoking = {
        name
        for name, text in texts.items()
        if any("-m pytest" in line for line in text.splitlines() if not line.startswith("#"))
    }
    assert checked, "no pytest recipe line found in Makefile or mk/*.mk: is the scan broken?"
    assert invoking <= checked, "a file runs `-m pytest` but has no checked recipe line: is the scan broken?"
    assert basetemp_problems(texts) == []


def test_compliant_recipe_with_mkdir_passes() -> None:
    line = "mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/zz-demo_1 tools/x -q"
    assert basetemp_problems(demo(line)) == []


@pytest.mark.parametrize(
    "recipe",
    [
        "$(PY) -m pytest tools/x -q",
        "@cd tools && pytest -q",
        ".local/venv-dev/bin/pytest tools/x -q",
        "$(VENV)/bin/pytest tools/x -q",
        "$(PY) -mpytest tools/x -q",
        "py.test tools/x -q",
        "$(PY) -m pytest tools/x -q --cov-report= && echo .local/pytest-tmp",
    ],
)
def test_pytest_call_without_basetemp_fails(recipe: str) -> None:
    assert basetemp_problems(demo(recipe)) == ["mk/zz-demo.mk:2: 1 pytest call(s), 0 --basetemp"]


@pytest.mark.parametrize(
    "value",
    [
        "/srv/x",
        "build/x",
        ".local/pytest-tmp/",
        ".local/pytest-tmp/.",
        ".local/pytest-tmp/../../x",
        ".local/pytest-tmp/a/b",
        ".local/pytest-tmp/$(TARGET)",
        ".local/pytest-tmp-x",
        "",
    ],
)
def test_basetemp_that_is_not_one_plain_dir_fails(value: str) -> None:
    problems = basetemp_problems(demo(f"$(PY) -m pytest --basetemp={value} tools/x -q"))
    assert f"mk/zz-demo.mk:2: --basetemp={value} is not one plain dir .local/pytest-tmp/<name>" in problems


def test_shared_basetemp_in_two_targets_fails() -> None:
    line = "\t$(PY) -m pytest --basetemp=.local/pytest-tmp/same tools/x\n"
    texts = {"mk/a.mk": "a:\n" + line, "mk/b.mk": "b:\n" + line}
    assert basetemp_problems(texts) == ["mk/b.mk:2: --basetemp=.local/pytest-tmp/same already used at mk/a.mk:2"]


def test_basetemp_nested_in_another_calls_dir_fails() -> None:
    texts = {
        "mk/a.mk": "a:\n\t$(PY) -m pytest --basetemp=.local/pytest-tmp/check-hygiene x\n",
        "mk/b.mk": "b:\n\t$(PY) -m pytest --basetemp=.local/pytest-tmp/check-hygiene/x y\n",
    }
    assert basetemp_problems(texts) == [
        "mk/b.mk:2: --basetemp=.local/pytest-tmp/check-hygiene/x is not one plain dir .local/pytest-tmp/<name>"
    ]


def test_second_call_on_one_line_needs_its_own_basetemp() -> None:
    texts = {"mk/a.mk": "a:\n\t$(PY) -m pytest --basetemp=.local/pytest-tmp/a x && $(PY) -m pytest y\n"}
    assert basetemp_problems(texts) == ["mk/a.mk:2: 2 pytest call(s), 1 --basetemp"]


def test_second_call_by_path_on_a_checked_line_fails() -> None:
    texts = {"mk/a.mk": "a:\n\t$(PY) -m pytest --basetemp=.local/pytest-tmp/a x && .local/venv-dev/bin/pytest y\n"}
    assert basetemp_problems(texts) == ["mk/a.mk:2: 2 pytest call(s), 1 --basetemp"]


def test_pytest_variable_definition_fails() -> None:
    texts = {"mk/a.mk": "PYTEST = $(PY) -m pytest\na:\n\t$(PYTEST) tools/x -q\n"}
    assert basetemp_problems(texts) == ["mk/a.mk:1: pytest mentioned outside a checked recipe line"]


def test_space_indented_continuation_fails() -> None:
    texts = {"mk/a.mk": "a:\n\t$(PY) -m \\\n    pytest tools/x -q\n"}
    assert basetemp_problems(texts) == ["mk/a.mk:3: pytest mentioned outside a checked recipe line"]


def test_target_line_naming_pytest_fails() -> None:
    texts = {"mk/a.mk": "a: pytest.ini\n\ttrue\nb: ; $(PY) -m pytest x\n"}
    assert basetemp_problems(texts) == [
        "mk/a.mk:1: pytest mentioned outside a checked recipe line",
        "mk/a.mk:3: pytest mentioned outside a checked recipe line",
    ]


def test_comment_lines_are_exempt() -> None:
    texts = {"mk/a.mk": "#: a — runs pytest\n# pytest without basetemp, py.test, -mpytest\na:\n\ttrue\n"}
    assert recipe_lines(texts) == []
    assert basetemp_problems(texts) == []


def test_pytest_subprocesses_in_tests_pass_basetemp() -> None:
    """A test that starts `python -m pytest` passes `--basetemp` on the same argv line (under its own tmp tree)."""
    roots = [TOP / "tools", TOP / "hermes-plugin/tests"]
    hits = [
        f"{path.relative_to(TOP)}:{number}"
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if SUBPROCESS_ARGV in line and '"--basetemp=' not in line
    ]
    assert hits == []
