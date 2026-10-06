# SPDX-License-Identifier: Apache-2.0
"""Tree and CLI tests of tools/quality/check_readmes.py (T1.103): scratch repositories, a copy of the real tree
mutated per test, and the real tree itself.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_readmes.py"
TOP = SCRIPT.parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_readmes_tree_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = _load()
MODULES = cr.MODULES_FILE
GOOD = "# T\n\n## Purpose\n\np\n\n## Public API\n\na\n\n## Invariants\n\ni `test-hermes/`\n"
OVERRIDE_LINE = "protocol | length-override | reason: protocol specification"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False, env=env
    )


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def _findings(root: Path) -> list[str]:
    _git(root, "add", "-A")
    findings, _count = cr.check_tree(root)
    return list(findings)


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Every tracked or new (not ignored) file of the real tree, as a fresh repository. Top-level dot-directories
    (the development repository's private notes among them) are skipped: the checker reads nothing there."""
    listing = _git(TOP, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    root = tmp_path_factory.mktemp("real")
    for name in filter(None, listing.split("\0")):
        source = TOP / name
        if (name.startswith(".") and "/" in name) or source.is_symlink() or not source.is_file():
            continue
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, root / name)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


@pytest.fixture
def real(template: Path, tmp_path: Path) -> Path:
    shutil.copytree(template, tmp_path / "r", symlinks=True)
    return tmp_path / "r"


# --- the real repository ---------------------------------------------------------------------------------------------


def test_real_tree_is_clean() -> None:
    findings, count = cr.check_tree(TOP)
    assert findings == []
    assert count >= len(cr.REQUIRED_MODULES)


def test_real_tree_cli() -> None:
    done = _run(TOP)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("check-readmes: OK (")


def test_copy_of_real_tree_is_clean(real: Path) -> None:
    assert _findings(real) == []


# --- protocol/README.md: the one length override (ENGINEERING.md section 1) -----------------------------------------


def test_protocol_passes_only_through_its_override(real: Path) -> None:
    lines = (real / MODULES).read_text(encoding="utf-8").splitlines()
    override = [line for line in lines if line.startswith("protocol |")]
    assert len(override) == 1 and "length-override" in override[0]
    (real / MODULES).write_text("\n".join(x for x in lines if x not in override) + "\n", encoding="utf-8")
    assert any(f.startswith("protocol: module dir not in") for f in _findings(real))
    (real / MODULES).write_text("\n".join(lines).replace(override[0], "protocol") + "\n", encoding="utf-8")
    findings = _findings(real)
    assert any(f.startswith("protocol/README.md:") and "limit 40" in f for f in findings), findings


@pytest.mark.parametrize("heading", ["## Purpose\n", "## Public API\n", "## Invariants\n"])
def test_protocol_without_a_heading_fails(real: Path, heading: str) -> None:
    _edit(real / "protocol/README.md", heading, "")
    assert any(f.startswith("protocol/README.md:") for f in _findings(real))


def test_protocol_headings_not_first_fail(real: Path) -> None:
    readme = real / "protocol/README.md"
    text = readme.read_text(encoding="utf-8")
    start, end = text.index("## Purpose\n"), text.index("## Envelope\n")
    rest = text[end:]
    readme.write_text(text[:start] + rest.replace("## Delivery\n", text[start:end] + "## Delivery\n"), "utf-8")
    findings = _findings(real)
    assert any("first `##` sections must be exactly" in f for f in findings), findings


@pytest.mark.parametrize(
    "line",
    [
        "protocol | length-override | ref: ENGINEERING.md §1",
        OVERRIDE_LINE,
        "protocol | length-override | reason: long | ref: ENGINEERING.md §1",
    ],
)
def test_override_without_reason_or_ref_exits_2(real: Path, line: str) -> None:
    modules = real / MODULES
    text = modules.read_text(encoding="utf-8")
    current = next(x for x in text.splitlines() if x.startswith("protocol |"))
    modules.write_text(text.replace(current, line), encoding="utf-8")
    _git(real, "add", "-A")
    done = _run(real)
    assert done.returncode == 2, done.stderr
    assert "length-override needs" in done.stderr


@pytest.mark.parametrize(
    ("ref", "finding"),
    [
        ("ENGINEERING.md §99", "protocol: ref ENGINEERING.md §99: ENGINEERING.md has no heading `99.`"),
        ("docs/missing.md §1", "protocol: ref docs/missing.md §1: docs/missing.md is not a tracked public file"),
    ],
)
def test_override_ref_must_resolve(real: Path, ref: str, finding: str) -> None:
    _edit(real / MODULES, "| ref: ENGINEERING.md §1", f"| ref: {ref}")
    assert _findings(real) == [finding]


def test_override_ref_to_an_untracked_file_fails(real: Path) -> None:
    (real / "notes.md").write_text("# Notes\n\n## 1. Rule\n\nok\n", encoding="utf-8")
    _edit(real / MODULES, "| ref: ENGINEERING.md §1", "| ref: notes.md §1")
    _git(real, "add", "-A")
    _git(real, "rm", "-q", "--cached", "notes.md")
    assert cr.check_tree(real)[0] == ["protocol: ref notes.md §1: notes.md is not a tracked public file"]


def test_override_ref_to_another_public_doc_passes(real: Path) -> None:
    (real / "docs/rules.md").write_text("# Rules\n\n## 3. Specs\n\nok\n", encoding="utf-8")
    _edit(real / MODULES, "| ref: ENGINEERING.md §1", "| ref: docs/rules.md §3")
    assert _findings(real) == []


# --- other modules of the real tree ----------------------------------------------------------------------------------


def test_fortyone_line_readme_fails(real: Path) -> None:
    readme = real / "mk/README.md"
    lines = readme.read_text(encoding="utf-8").splitlines()
    readme.write_text("\n".join(lines + ["- pad"] * (41 - len(lines))) + "\n", encoding="utf-8")
    assert _findings(real) == [
        "mk/README.md: 41 lines, limit 40 (a spec document needs a length-override, ENGINEERING.md §1)"
    ]


def test_missing_readme_fails(real: Path) -> None:
    _git(real, "rm", "-q", "-f", "tools/codegen/README.md")
    assert _findings(real) == ["tools/codegen/README.md: missing, untracked, a symlink or not UTF-8"]


def test_untracked_readme_fails(real: Path) -> None:
    _git(real, "rm", "-q", "--cached", "tools/mock-llm/README.md")
    findings, _ = cr.check_tree(real)
    assert findings == ["tools/mock-llm/README.md: missing, untracked, a symlink or not UTF-8"]


def test_symlinked_readme_fails(real: Path) -> None:
    (real / "tools/quality/README.md").unlink()
    os.symlink("../README.md", real / "tools/quality/README.md")
    assert _findings(real) == ["tools/quality/README.md: missing, untracked, a symlink or not UTF-8"]


def test_new_tool_dir_must_be_listed(real: Path) -> None:
    (real / "tools/newtool").mkdir()
    (real / "tools/newtool/run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert _findings(real) == [
        f"tools/newtool: module dir not in {MODULES} (give it a README and a line, or a part-of line)"
    ]


def test_new_plugin_package_must_be_listed(real: Path) -> None:
    (real / "hermes-plugin/other").mkdir()
    (real / "hermes-plugin/other/__init__.py").write_text("", encoding="utf-8")
    assert [f.split(":")[0] for f in _findings(real)] == ["hermes-plugin/other"]


def test_tests_dirs_are_not_modules(real: Path) -> None:
    (real / "tools/tests/extra.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert _findings(real) == []


def test_part_of_needs_a_mention_in_the_parent(real: Path) -> None:
    _edit(real / "tools/README.md", "`test-hermes/`", "test-hermes")
    assert _findings(real) == ["tools/README.md: does not name its part `test-hermes/`"]


@pytest.mark.parametrize("parent", ["mk", "tools/quality", "nothing"])
def test_part_of_must_point_to_a_listed_module_above(real: Path, parent: str) -> None:
    _edit(real / MODULES, "tools/test-hermes | part-of: tools", f"tools/test-hermes | part-of: {parent}")
    assert [f.split(":")[0] for f in _findings(real)] == ["tools/test-hermes"]


def test_listed_dir_without_files_fails(real: Path) -> None:
    _edit(real / MODULES, "\ntools\n", "\ntools\ntools/gone\n")
    assert _findings(real) == [f"tools/gone: listed in {MODULES} but holds no tracked file"]


# --- fail closed: exit 2 ---------------------------------------------------------------------------------------------


def test_missing_module_list_exits_2(real: Path) -> None:
    _git(real, "rm", "-q", "-f", MODULES)
    done = _run(real)
    assert done.returncode == 2 and "missing, untracked or unreadable" in done.stderr


def test_untracked_module_list_exits_2(real: Path) -> None:
    _git(real, "rm", "-q", "--cached", MODULES)
    assert _run(real).returncode == 2


def test_empty_module_list_exits_2(real: Path) -> None:
    (real / MODULES).write_text("# nothing\n", encoding="utf-8")
    assert _run(real).returncode == 2


def test_corrupt_index_exits_2(real: Path) -> None:
    (real / ".git/index").write_bytes(b"garbage\n")
    done = _run(real)
    assert done.returncode == 2 and "git" in done.stderr


def test_outside_a_repository_exits_2(tmp_path: Path) -> None:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path))
    (tmp_path / "plain").mkdir()
    assert _run(tmp_path / "plain", env=env).returncode == 2


def test_subdirectory_is_not_a_top(real: Path) -> None:
    with pytest.raises(cr.ReadmeError, match="not the top level"):
        cr.check_tree(real / "tools")


def test_arguments_exit_2(real: Path) -> None:
    assert _run(real, "--fix").returncode == 2


def test_cli_findings_exit_1_from_a_subdirectory(real: Path) -> None:
    _edit(real / "mk/README.md", "## Invariants\n", "## Rules\n")
    _git(real, "add", "-A")
    done = _run(real / "tools")
    assert done.returncode == 1
    assert "check-readmes: mk/README.md:" in done.stderr


def test_scratch_minimal_repo(tmp_path: Path) -> None:
    files = {
        MODULES: "mk\nprotocol\ntools\ntools/quality\ntools/test-hermes | part-of: tools\n",
        "tools/quality/README.md": GOOD,
        "mk/README.md": GOOD,
        "protocol/README.md": GOOD,
        "tools/README.md": GOOD,
        "tools/test-hermes/run.sh": "x\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    assert _findings(tmp_path) == []
    assert cr.check_tree(tmp_path)[1] == 4
