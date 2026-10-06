# SPDX-License-Identifier: Apache-2.0
"""CLI tests of tools/quality/check_hygiene.py (T1.102) on scratch repositories and on the real tree.

Planted keywords are assembled at run time (`KW_TODO`) so this file itself never holds a bare marker.
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
KW_TODO = "TO" + "DO"
GOOD_GLOSSARY = (
    "# Glossary\n\n| Term | Meaning | Not to be called |\n|---|---|---|\n"
    "| **app** | The phone app | client |\n| **lane** | A channel \\| routed | topic |\n"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_hygiene_cli_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hy = _load()


# --- the real repository: the gate must be green on its own tree -------------------------------------------------


def test_real_tree_is_clean() -> None:
    findings, _scope = hy.check_tree(TOP)
    assert findings == [], findings


def test_real_gate_start_file_is_placeholder_or_a_commit() -> None:
    start = hy.parse_gate_start((TOP / hy.GATE_START_FILE).read_text(encoding="utf-8"))
    if start is not None:
        assert hy.gate_start(TOP) == start


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CEILING_DIRECTORIES=str(tmp_path),
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    return env


def _git(repo: Path, *args: str) -> None:
    command = ["git", *args]
    subprocess.run(command, cwd=repo, env=_env(repo.parent), check=True, capture_output=True)


def _commit(repo: Path, subject: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, env=_env(cwd.parent), capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _commit(root, "T1.1: init", {"docs/glossary.md": GOOD_GLOSSARY, "a.py": "x = 1\n"})
    return root


def test_cli_clean_tree(repo: Path) -> None:
    done = _run(repo)
    assert done.returncode == 0, done.stderr
    assert "OK" in done.stdout


def test_cli_planted_todo_fails(repo: Path) -> None:
    _commit(repo, "T1.1: plant", {"pkg/b.py": f"x = 1\n# {KW_TODO} fix\n"})
    done = _run(repo)
    assert done.returncode == 1
    assert "pkg/b.py:2:" in done.stderr


def test_cli_planted_todo_with_id_passes(repo: Path) -> None:
    _commit(repo, "T1.1: plant", {"pkg/b.py": f"# {KW_TODO}(T1.23): split\n"})
    assert _run(repo).returncode == 0


def test_cli_skips_untracked_team_and_binary(repo: Path) -> None:
    (repo / "untracked.py").write_text(f"# {KW_TODO} fix\n", encoding="utf-8")
    _commit(repo, "T1.1: plant", {".team/n.md": f"<!-- {KW_TODO} -->\n"})
    (repo / "img.png").write_bytes(b"\x89PNG\0\0# " + KW_TODO.encode() + b"\n")
    _git(repo, "add", "img.png")
    assert _run(repo).returncode == 0


def test_cli_duplicate_glossary_term_fails(repo: Path) -> None:
    _commit(repo, "T1.1: dup", {"docs/glossary.md": GOOD_GLOSSARY + "| **lane** | A row | x |\n"})
    done = _run(repo)
    assert done.returncode == 1
    assert "docs/glossary.md:7:" in done.stderr


def test_cli_missing_glossary_fails(repo: Path) -> None:
    _git(repo, "rm", "-q", "docs/glossary.md")
    done = _run(repo)
    assert done.returncode == 1
    assert "docs/glossary.md" in done.stderr


def test_cli_commit_range(repo: Path) -> None:
    _commit(repo, "fix stuff", {})
    _commit(repo, "docs: fine", {})
    done = _run(repo, "--commits", "HEAD~2..HEAD")
    assert done.returncode == 1
    assert "fix stuff" in done.stderr and "docs: fine" not in done.stderr
    ok = _run(repo, "--commits", "HEAD~1..HEAD")
    assert ok.returncode == 0, ok.stderr
    assert "1 commit" in ok.stdout


@pytest.mark.parametrize("rng", ["HEAD..HEAD", "..HEAD", "HEAD..", "..", "^HEAD"])
def test_cli_empty_range_fails_closed(repo: Path, rng: str) -> None:
    done = _run(repo, f"--commits={rng}")
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "check-hygiene:" in done.stderr


@pytest.mark.parametrize("bad", ["nosuchref..HEAD", "--all", "-n1"])
def test_cli_bad_range_fails_closed(repo: Path, bad: str) -> None:
    assert _run(repo, f"--commits={bad}").returncode == 2


def test_cli_outside_git_fails_closed(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text(f"# {KW_TODO} fix\n", encoding="utf-8")
    done = _run(plain)
    assert done.returncode == 2
    assert "check-hygiene:" in done.stderr


def test_cli_corrupt_index_fails_closed(repo: Path) -> None:
    (repo / ".git" / "index").write_bytes(b"garbage\n")
    assert _run(repo).returncode == 2


def test_cli_no_tracked_files_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-q")
    assert _run(root).returncode == 2


# --- commit gate start (--commits without a range) ----------------------------------------------------------------


def _head(repo: Path) -> str:
    argv = ["git", "rev-parse", "HEAD"]
    done = subprocess.run(argv, cwd=repo, env=_env(repo.parent), capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _record_start(repo: Path, text: str) -> None:
    _commit(repo, "chore: record gate start", {hy.GATE_START_FILE: text})


def test_cli_gate_start_exempts_history(repo: Path) -> None:
    _commit(repo, "QA: old history", {})
    start = _head(repo)
    _record_start(repo, f"# gate start\n{start}\n")
    _commit(repo, "T1.06a: sub-id", {})
    done = _run(repo, "--commits")
    assert done.returncode == 0, done.stderr
    assert f"2 commits in {start}..HEAD" in done.stdout
    _commit(repo, "Review: new commit", {})
    bad = _run(repo, "--commits")
    assert bad.returncode == 1 and "Review: new commit" in bad.stderr and "old history" not in bad.stderr


@pytest.mark.parametrize("text", [None, "", "# no hash yet\n", "HEAD\n", "0" * 40 + "\n"])
def test_cli_gate_start_missing_empty_or_invalid_fails_closed(repo: Path, text: str | None) -> None:
    if text is not None:
        _record_start(repo, text)
    done = _run(repo, "--commits")
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "commit-gate-start.txt" in done.stderr or "rev-parse" in done.stderr


def test_cli_gate_start_at_head_fails_closed(repo: Path) -> None:
    head = _head(repo)
    (repo / "tools" / "quality").mkdir(parents=True)
    (repo / hy.GATE_START_FILE).write_text(head + "\n", encoding="utf-8")
    done = _run(repo, "--commits")
    assert done.returncode == 2 and "no commits" in done.stderr, (done.stdout, done.stderr)
