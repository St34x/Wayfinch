# SPDX-License-Identifier: Apache-2.0
"""Edge cases of the module README gate: empty sections, the public Python floor of the checker itself, and
the tools/README.md list of shell tests (every one runs in `make test` or is documented as manual; the
`make test` list is exactly the scripts named in the mk/*.mk recipes).
"""

import ast
import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

TOP = Path(__file__).resolve().parents[3]
SCRIPT = TOP / "tools/quality/check_readmes.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_readmes_edges_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = _load()
HEAD = "<!-- SPDX-License-Identifier: Apache-2.0 -->\n# Title\n\n## Purpose\n\n"
TAIL = "\n## Public API\n\n- `f()`\n\n## Invariants\n\n- never X\n"


@pytest.mark.parametrize("body", ["", "<!-- note -->\n", "\u00a0\n", "  \n\n"])
def test_section_without_content_is_empty(body: str) -> None:
    assert any("is empty" in p for p in cr.check_readme(HEAD + body + TAIL, spec=False))


def test_crlf_lines_count_like_lf() -> None:
    text = (HEAD + "x\n" + TAIL).replace("\n", "\r\n") + "\r\n" * 40
    assert any("limit 40" in p for p in cr.check_readme(text, spec=False))


def test_code_inside_a_fence_is_content() -> None:
    assert cr.check_readme(HEAD + "```\nx\n```\n" + TAIL, spec=False) == []


def test_headings_inside_a_fence_do_not_count() -> None:
    fenced = "# Title\n\n```\n## Purpose\nx\n## Public API\nx\n## Invariants\nx\n```\n"
    assert cr.check_readme(fenced, spec=False)


@pytest.mark.parametrize("body", ["---\n", "***\n", "- - -\n", "```\n```\n", "~~~\n~~~\n<!-- x -->\n"])
def test_section_with_only_markup_is_empty(body: str) -> None:
    assert any("is empty" in p for p in cr.check_readme(HEAD + body + TAIL, spec=False))


def test_checker_parses_with_the_python_311_grammar() -> None:
    ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT), feature_version=(3, 11))


def test_every_tools_shell_test_is_wired_or_documented_as_manual() -> None:
    recipes = "\n".join(
        line
        for path in sorted((TOP / "mk").glob("*.mk"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("\t")
    )
    readme = (TOP / "tools/README.md").read_text(encoding="utf-8")
    manual = set(re.findall(r"`(?:tools/)?tests/(test_[a-z0-9_]+\.sh)`[^\n]*(?:by hand|manual)", readme))
    missing = [
        path.name
        for path in sorted((TOP / "tools/tests").glob("test_*.sh"))
        if f"tools/tests/{path.name}" not in recipes and path.name not in manual
    ]
    assert not missing, missing


@pytest.mark.parametrize("glob", ["test_check_hygiene", "test_check_readmes"])
def test_hygiene_fragment_fails_on_an_empty_test_glob(tmp_path: Path, glob: str) -> None:
    """mk/hygiene.mk with no file matching one of its test globs exits 2 instead of running pytest on nothing."""
    make = shutil.which("make")
    if make is None:
        pytest.skip("make not installed")
    (tmp_path / "mk").mkdir()
    shutil.copyfile(TOP / "mk/hygiene.mk", tmp_path / "mk/hygiene.mk")
    tests = tmp_path / "tools/quality/tests"
    tests.mkdir(parents=True)
    other = {"test_check_hygiene": "test_check_readmes", "test_check_readmes": "test_check_hygiene"}[glob]
    (tests / f"{other}_x.py").write_text("def test_ok() -> None:\n    pass\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "PY"}}
    done = subprocess.run(
        [make, "-f", "mk/hygiene.mk", "PY=/bin/true", "check-hygiene"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode != 0, done.stdout
    assert f"check-hygiene: no tools/quality/tests/{glob}*.py" in done.stderr


def _recipe_scripts() -> set[str]:
    lines = [
        line
        for path in sorted((TOP / "mk").glob("*.mk"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("\t")
    ]
    return set(re.findall(r"tools/tests/(test_[a-z0-9_]+\.sh)", "\n".join(lines)))


def test_tools_readme_names_exactly_the_shell_tests_make_test_runs() -> None:
    readme = (TOP / "tools/README.md").read_text(encoding="utf-8")
    claim = re.search(r"`make test` runs (.*?)\. Run by hand", readme, re.DOTALL)
    assert claim is not None, "tools/README.md no longer says which shell tests `make test` runs"
    listed = set(re.findall(r"`(test_[a-z0-9_]+\.sh)`", claim[1]))
    assert listed == _recipe_scripts()
