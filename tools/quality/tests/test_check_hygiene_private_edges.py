# SPDX-License-Identifier: Apache-2.0
"""Adversarial inputs for rule (d) of tools/quality/check_hygiene.py.

Planted references are assembled at run time (`ADR`, `ADR_DIR`, `TEAM`, `QA`, `REVIEW`) so this file holds none.
Case and separator variants fail like the planted forms. One gap stays pinned as a
strict xfail: a tracked symlink whose target is inside the private folder (out of scope, see its reason).
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_hygiene.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_hygiene_private_edges_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hy = _load()
TEAM = hy.PRIVATE_DIR
ADR = "AD" + "R"
ADR_DIR = "ad" + "r/"
QA = "q" + "a"
REVIEW = "re" + "view"
GAP_SYMLINK = (
    "not required: a tracked symlink is skipped and its target never read; the export refuses symlinks, so no target"
    " text can be published"
)


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("docs/a.md", f"See `{TEAM}{ADR_DIR}`."),
        ("docs/a.md", f"See `{ADR}-012`."),
        ("docs/a.md", f"[the record](../{TEAM}{ADR_DIR}012-x.md)"),
        ("docs/a.md", f"[QA]({QA}-T1.99.md)"),
        ("docs/a.md", f"Found in {REVIEW}-T1.99.md."),
        ("docs/a.md", f"<!-- {ADR}-012 -->"),
        ("hermes-plugin/glasses/README.md", f"Notes live in the {TEAM.rstrip('/')} folder."),
        ("mk/x.mk", f"# see {ADR_DIR}012-x.md"),
        ("tools/quality/modules.txt", f"protocol | length-override | reason: spec | ref: {ADR}-005"),
        ("README.md", f"see docs/{TEAM}x.md"),
    ],
)
def test_planted_form_fails(path: str, line: str) -> None:
    findings = hy.private_ref_findings(path, f"ok\n{line}\n")
    assert findings, line
    assert all(f.startswith(f"{path}:2: private reference ") for f in findings), findings


@pytest.mark.parametrize("ref", [f"{ADR}-012", f"{TEAM}kb", f"{QA}-T1.99.md"])
def test_reference_at_end_of_file_without_newline_fails(ref: str) -> None:
    assert hy.private_ref_findings("docs/a.md", f"ok\nlast words {ref}") != []


@pytest.mark.parametrize(
    "line",
    [
        f"see {ADR.lower()}-012",
        f"see {ADR.title()}-012",
        f"see {QA.upper()}-T1.99.md",
        f"see {REVIEW.title()}-T1.04.md",
        f"see {TEAM.title()}kb/x.md",
    ],
)
def test_case_variant_fails(line: str) -> None:
    assert hy.private_ref_findings("docs/a.md", line + "\n") != []


# --- CLI on a scratch repository ------------------------------------------------------------------------------------

GLOSSARY = "# Glossary\n\n| Term | Meaning |\n|---|---|\n| **app** | The phone app |\n"


def _env(repo: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOME=str(repo.parent),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CEILING_DIRECTORIES=str(repo.parent),
    )
    return env


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "glossary.md").write_text(GLOSSARY, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, env=_env(root), check=True)
    return root


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, env=_env(repo), capture_output=True, text=True)


@pytest.mark.xfail(strict=True, reason=GAP_SYMLINK)
def test_cli_symlink_into_private_folder_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs" / "qa.md").symlink_to(f"../{TEAM}{QA}-T1.99.md")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=_env(repo), check=True)
    done = _run(repo)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "docs/qa.md" in done.stderr


def test_cli_intent_to_add_file_is_checked(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=repo, env=_env(repo), check=True)
    (repo / "docs" / "new.md").write_text(f"see {TEAM}kb/x.md\n", encoding="utf-8")
    subprocess.run(["git", "add", "-N", "docs/new.md"], cwd=repo, env=_env(repo), check=True)
    done = _run(repo)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "docs/new.md:1: private reference" in done.stderr


def test_cli_run_from_subdirectory_checks_whole_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text(f"See {ADR}-029.\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=_env(repo), check=True)
    done = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo / "docs", env=_env(repo), capture_output=True, text=True
    )
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "README.md:1: private reference" in done.stderr
