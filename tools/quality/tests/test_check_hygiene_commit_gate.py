# SPDX-License-Identifier: Apache-2.0
"""Commit mode of tools/quality/check_hygiene.py and task-id forms.

Subject allowlist exactness, task sub-ids, range syntax, and the commit gate start file (GATE_START_FILE): the
merge-step recipe on the real placeholder, and every missing/invalid form failing closed with exit 2.
Planted keywords are assembled at run time (KW_TODO) so this file never holds a bare marker.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_hygiene.py"
TOP = SCRIPT.parents[2]
KW_TODO = "TO" + "DO"
GIT = shutil.which("git") or "git"
ROUND = "rou" + "nd"
EXPECTED_PREFIXES = {
    "merge", "design", "docs", "plan", "tasks", "adr", "research", "spike", "mockups", "qa", "review", "status",
    "night", "chore", "fix", "feat", "test", "refactor",
}  # fmt: skip


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_hygiene_commit_gate_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hy = _load()


# --- (b) subject allowlist: exactly the expected prefixes ------------------------------------------------------


def test_prefix_list_is_exactly_the_expected_list() -> None:
    assert set(hy.SUBJECT_PREFIXES) == EXPECTED_PREFIXES
    assert len(hy.SUBJECT_PREFIXES) == len(EXPECTED_PREFIXES)


@pytest.mark.parametrize("prefix", sorted(EXPECTED_PREFIXES))
def test_every_listed_prefix_passes(prefix: str) -> None:
    assert hy.subject_ok(f"{prefix}: x")


@pytest.mark.parametrize(
    "subject",
    [
        "Plan: x", "QA: x", "Review: x", "Merge: x", f"QA {ROUND} 2: x", f"qa {ROUND} 2: x", "Fix: x", "fixes: x",
        "feat(ui): x", "tests: x", "refactoring: x", "FEAT: x",
        "doc: x", "merges: x", "mergeable: x", "reviewer: x", "chore(ci): x", "wip: x", "ci: x",
        "T1.06ab: x", "T1.06A: x", "T1: x", "t1.06: x", "P.4a: x", "merge:x", "merge: ", "merge:  x", " merge: x",
        "Merge branch 'x'", "Merge pull request #1 from a/b", 'Revert "T1.06a: x"', "fixup! T1.06a: x",
    ],
)  # fmt: skip
def test_near_miss_subjects_fail(subject: str) -> None:
    assert not hy.subject_ok(subject)


@pytest.mark.parametrize("subject", ["T1.06a: x", "T1.15e: x", "T1.102: x", "P.4: x", "S05: x", "S05a: x"])
def test_task_id_subjects_pass(subject: str) -> None:
    assert hy.subject_ok(subject)


@pytest.mark.parametrize(("task", "ok"), [("T1.06a", True), ("T1.102", True), ("T1.06ab", False), ("T1.06A", False)])
def test_marker_ids(task: str, ok: bool) -> None:
    assert (hy.todo_findings("a.py", f"# {KW_TODO}({task}): x\n") == []) is ok


# --- range syntax ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("rng", ["", "-x", "..HEAD", "HEAD..", "..", "...", "...HEAD", "HEAD...", " ..HEAD", "a.. "])
def test_bad_range_syntax_is_rejected(rng: str) -> None:
    with pytest.raises(hy.HygieneError):
        hy.check_range_syntax(rng)


# --- gate start file: parsing and the merge-step recipe ---------------------------------------------------------


H40 = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("", None),
        ("# only comments\n\n", None),
        (f"{H40}\n", H40),
        (f"  {H40}  \r\n", H40),
        ("a" * 64 + "\n", "a" * 64),
    ],
)
def test_gate_start_parses(text: str, want: str | None) -> None:
    assert hy.parse_gate_start(text) == want


@pytest.mark.parametrize(
    "text",
    [H40.upper(), H40[:7], f"{H40}\n{H40}", f"\ufeff{H40}", f"{H40} # merge of T1.102", "HEAD", "a" * 41],
)
def test_gate_start_rejects_anything_but_one_full_hash(text: str) -> None:
    with pytest.raises(hy.HygieneError):
        hy.parse_gate_start(text + "\n")


def test_merge_step_recipe_on_the_real_file() -> None:
    """`git rev-parse HEAD >> <file>` (the documented merge step) on the shipped placeholder yields that hash."""
    real = (TOP / hy.GATE_START_FILE).read_text(encoding="utf-8")
    placeholder = (
        real
        if hy.parse_gate_start(real) is None
        else "".join(line + "\n" for line in real.splitlines() if line.lstrip().startswith("#"))
    )
    assert hy.parse_gate_start(placeholder) is None
    assert hy.parse_gate_start(placeholder + H40 + "\n") == H40


# --- gate start file: CLI on scratch repositories ---------------------------------------------------------------


def _env(tmp: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOME=str(tmp), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CEILING_DIRECTORIES=str(tmp),
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )  # fmt: skip
    return env


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run([GIT, *args], cwd=repo, env=_env(repo.parent), check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _run(repo: Path, *args: str, sub: str = "") -> tuple[int, str]:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=repo / sub, env=_env(repo.parent), capture_output=True, text=True
    )
    return done.returncode, done.stdout + done.stderr


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """History `QA: old` -> `merge: T1.102 ...` (the gate start); gate file present as the placeholder."""
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "commit", "-q", "--allow-empty", "-m", "QA: exempt history")
    _git(root, "commit", "-q", "--allow-empty", "-m", "merge: T1.102 hygiene gate")
    gate = root / hy.GATE_START_FILE
    gate.parent.mkdir(parents=True)
    gate.write_text("# placeholder\n", encoding="utf-8")
    return root


def _record(repo: Path) -> str:
    """The merge step: append the merge commit hash and commit it as a `chore:` follow-up."""
    start = _git(repo, "rev-parse", "HEAD")
    with (repo / hy.GATE_START_FILE).open("a", encoding="utf-8") as handle:
        handle.write(start + "\n")
    _git(repo, "add", hy.GATE_START_FILE)
    _git(repo, "commit", "-q", "-m", "chore: record commit gate start")
    return start


def test_placeholder_fails_closed(repo: Path) -> None:
    rc, out = _run(repo, "--commits")
    assert rc == 2 and "no commit recorded" in out, out


def test_recorded_before_commit_has_nothing_to_check(repo: Path) -> None:
    (repo / hy.GATE_START_FILE).write_text(_git(repo, "rev-parse", "HEAD") + "\n", encoding="utf-8")
    rc, out = _run(repo, "--commits")
    assert rc == 2 and "no commits" in out, out


@pytest.mark.parametrize("args", [("--commits",), ("--commits", "GATE_START..HEAD")])
def test_recorded_start_checks_only_newer_commits(repo: Path, args: tuple[str, ...]) -> None:
    start = _record(repo)
    rc, out = _run(repo, *args)
    assert rc == 0 and f"1 commit in {start}..HEAD" in out, out
    rc, out = _run(repo, *args, sub="tools")
    assert rc == 0, out
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Merge branch 'x'")
    rc, out = _run(repo, *args)
    assert rc == 1 and "Merge branch" in out and "QA: exempt" not in out, out


def _plant(repo: Path, kind: str) -> None:
    gate = repo / hy.GATE_START_FILE
    gate.unlink()
    if kind == "dir":
        gate.mkdir()
    elif kind == "symlink":
        gate.symlink_to(os.devnull)
    elif kind != "missing":
        (repo / "blob.txt").write_text("blob\n", encoding="utf-8")
        objects = {
            "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
            "blob": _git(repo, "hash-object", "-w", "blob.txt"),
            "unknown": "1" * 40,
            "binary": "\0" + _git(repo, "rev-parse", "HEAD"),
            "uppercase": _git(repo, "rev-parse", "HEAD").upper(),
            "short": _git(repo, "rev-parse", "--short", "HEAD"),
        }
        gate.write_text(objects[kind] + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    "kind", ["missing", "dir", "symlink", "tree", "blob", "unknown", "binary", "uppercase", "short"]
)
def test_invalid_gate_start_fails_closed(repo: Path, kind: str) -> None:
    _record(repo)
    _plant(repo, kind)
    rc, out = _run(repo, "--commits")
    assert rc == 2 and "check-hygiene:" in out, out


def test_all_zero_base_fails_closed(repo: Path) -> None:
    """GitHub sends 40 zeros as `before` on a new branch push."""
    rc, out = _run(repo, "--commits", "0" * 40 + "..HEAD")
    assert rc == 2, out
