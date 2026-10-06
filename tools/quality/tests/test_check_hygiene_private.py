# SPDX-License-Identifier: Apache-2.0
"""Tests of rule (d) of tools/quality/check_hygiene.py (T1.141): no private reference in the published tree.

Planted references are assembled at run time (`ADR`, `ADR_DIR`, `TEAM`, `QA`, `REVIEW`) so this file holds none
and needs no exemption. The real tree is checked by test_check_hygiene_cli.py::test_real_tree_is_clean.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_hygiene.py"
TOP = SCRIPT.parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_hygiene_private_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hy = _load()
TEAM = hy.PRIVATE_DIR
ADR = "AD" + "R"
QA = "q" + "a"
ADR_DIR = "ad" + "r/"
REVIEW = "re" + "view"
EXEMPT = sorted(hy.PRIVATE_FOLDER_EXEMPT)


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("a.py", f"# see {ADR}-015 for the reasoning"),
        ("a.py", f'"""Registry ({ADR}-005, {ADR}-025 step 1)."""'),
        ("a.py", f"# ({ADR}-003/020/022)"),
        ("docs/x.md", f"Decided in [the record]({TEAM}{ADR_DIR}015-long-answers.md)."),
        ("docs/x.md", f"Decided in {ADR_DIR}015 of the team docs."),
        ("schema.json", f'  "description": "Never expires ({ADR}-024)."'),
        ("a.py", f"# open finding in {TEAM}{QA}-T1.98.md"),
        ("a.py", f"# the {TEAM.rstrip('/')} folder holds the notes"),
        ("Makefile", f"# {TEAM}kb/INDEX.md"),
        ("tests/t.py", f"# fix round of {REVIEW}-T1.04 + {QA}-T1.04"),
        ("tests/t.py", f"# {QA}-T1.06a sub-id report"),
        ("tests/t.py", f"# {QA}-P.04 and {QA}-S05 reports"),
    ],
)
def test_private_reference_fails(path: str, line: str) -> None:
    findings = hy.private_ref_findings(path, f"ok\n{line}\n")
    assert findings, line
    assert all(f.startswith(f"{path}:2: private reference ") for f in findings), findings


@pytest.mark.parametrize(
    "ref",
    [
        f"{ADR.lower()}-015",
        f"{ADR.title()}-015",
        f"{ADR} 015",
        f"{ADR}015",
        f"{ADR}/015",
        f"{ADR} #15",
        f"{ADR}_015",
        f"{TEAM.upper()}kb",
        f"{TEAM.title()}kb",
        f"{QA.upper()}-T1.98.md",
        f"{REVIEW.title()}-T1.04",
        f"{QA}-t1.98",
        f"{QA}_T1.99.md",
    ],
)
def test_spelling_and_case_variants_fail(ref: str) -> None:
    assert len(hy.private_ref_findings("docs/a.md", f"see {ref} here\n")) == 1, ref


@pytest.mark.parametrize(
    "path", [f"docs/{QA}-T1.98.md", f"docs/{ADR_DIR}015-x.md", f"docs/{TEAM}x.md", f"docs/{REVIEW.title()}_T1.04.md"]
)
def test_private_path_name_fails(path: str) -> None:
    findings = hy.private_name_findings(path)
    assert findings and all(f.startswith(f"{path}: private reference ") for f in findings), findings


@pytest.mark.parametrize("path", ["docs/adr.md", "docs/address/1.md", f"tools/{QA}/test_a.py", "docs/team/x.md"])
def test_ordinary_path_name_passes(path: str) -> None:
    assert hy.private_name_findings(path) == []


@pytest.mark.parametrize(
    "line",
    [
        "adr: commit subject prefix",
        f"an {ADR} without a number; {ADR}s in general",
        "address, readr/1 and xadr/2 are not decision records",
        "teams, .teamcity, my-team/ and steam/ are other words",
        f"{QA}-tests and review-board are no report names",
        "Purpose: the README of a module (T1.103)",
    ],
)
def test_look_alikes_pass(line: str) -> None:
    assert hy.private_ref_findings("docs/a.md", line + "\n") == []


def test_one_finding_per_reference() -> None:
    findings = hy.private_ref_findings("a.py", f"# {ADR}-001 and {ADR}-002, {TEAM}x\n")
    assert len(findings) == 3


@pytest.mark.parametrize("path", EXEMPT)
def test_exempt_gate_file_may_name_the_folder_only(path: str) -> None:
    assert hy.private_ref_findings(path, f"skip {TEAM}* here\n") == []
    assert len(hy.private_ref_findings(path, f"# {ADR}-021\n")) == 1
    assert len(hy.private_ref_findings(path, f"# {TEAM}{QA}-T1.98.md\n")) == 1


@pytest.mark.parametrize("path", EXEMPT)
def test_every_exemption_is_still_needed(path: str) -> None:
    """A stale exemption fails: the file must exist and still name the private folder."""
    text = (TOP / path).read_text(encoding="utf-8")
    assert hy.private_ref_findings(path + ".copy", text) != [], f"{path}: exemption no longer needed"


# --- CLI on scratch repositories ------------------------------------------------------------------------------------

GLOSSARY = "# Glossary\n\n| Term | Meaning |\n|---|---|\n| **app** | The phone app |\n"


def _git(repo: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update(
        HOME=str(repo.parent),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CEILING_DIRECTORIES=str(repo.parent),
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for name, text in {"docs/glossary.md": GLOSSARY, **files}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
        _git(root, "add", name)
    return root


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True)


def test_cli_planted_reference_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pkg/a.py": f"x = 1\n# see {TEAM}{QA}-T1.98.md\n", "README.md": f"See {ADR}-029.\n"})
    done = _run(repo)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "pkg/a.py:2: private reference" in done.stderr
    assert "README.md:1: private reference" in done.stderr


def test_cli_private_path_name_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {f"docs/{QA.upper()}-T1.98.md": "Plain text.\n"})
    done = _run(repo)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert f"docs/{QA.upper()}-T1.98.md: private reference" in done.stderr


def test_cli_private_folder_itself_is_not_checked(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {f"{TEAM}{ADR_DIR}029.md": f"# {ADR}-029, see {TEAM}kb/\n", "a.py": "x = 1\n"})
    done = _run(repo)
    assert done.returncode == 0, done.stderr
