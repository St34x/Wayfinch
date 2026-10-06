# SPDX-License-Identifier: Apache-2.0
"""Adversarial inputs for tools/quality/check_hygiene.py.

Planted keywords are assembled at run time (KW_*) so this file never holds a bare marker.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

QUALITY = Path(__file__).resolve().parents[1]
SCRIPT = QUALITY / "check_hygiene.py"
TOP = QUALITY.parents[1]
KW_TODO = "TO" + "DO"
KW_FIXME = "FIX" + "ME"
KW_XXX = "X" + "XX"
GLOSSARY = "# Glossary\n\n| Term | Meaning | Not to be called |\n|---|---|---|\n| **app** | The phone app | client |\n"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_hygiene_edges_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hy = _load()


# --- (a) markers: comments vs strings per file type -------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "text", "lines"),
    [
        ("a.py", f"x = 1\r\n# {KW_TODO} crlf\r\n", [2]),
        ("a.py", f"x = 1  # see {KW_XXX} here\n", [1]),
        ("a.sh", f"\tfoo  # {KW_FIXME}\n", [1]),
        ("a.yaml", f"key: value  # {KW_TODO} later\n", [1]),
        ("a.yml", f"  # {KW_XXX}\n", [1]),
        ("a.toml", f"# {KW_TODO}\n", [1]),
        ("A.kt", f"fun f() = 1 // {KW_TODO} later\n", [1]),
        ("A.kt", f"/**\n * Doc.\n * {KW_FIXME}: hack\n */\n", [3]),
        ("docs/a.md", f"text\n<!-- {KW_TODO} at end of file", [2]),
        ("res/v.xml", f"<a/>\n<!--\n{KW_XXX}\n-->\n", [3]),
        ("a.py", f"# {KW_TODO}-list\n", [1]),
    ],
)
def test_markers_in_comments_are_found(path: str, text: str, lines: list[int]) -> None:
    found = hy.todo_findings(path, text)
    assert [int(f.split(":")[1]) for f in found] == lines, found


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("a.py", f's = "{KW_TODO}"\n'),
        ("a.py", f'url = "http://x/#{KW_TODO}"\n'),
        ("a.py", f"# {KW_TODO}S and {KW_TODO}_LIST and {KW_XXX}X are other words\n"),
        ("a.sh", f'echo "${{#{KW_TODO}}}" "$#"\n'),
        ("a.yaml", f'key: "{KW_TODO}"\nother: x#{KW_TODO}\n'),
        ("a.json", f'{{"kind": "{KW_TODO}", "c": "x # {KW_TODO}"}}\n'),
        ("A.kt", f'val u = "https://example.com/{KW_TODO}"\n'),
        ("docs/a.md", f"Prose {KW_TODO} and\n```sh\n# {KW_TODO} in a code fence\n```\n"),
        ("protocol/README.md", f"| `meeting.hint` | `{KW_TODO}` |\n"),
        ("a.py", f"# {KW_TODO}(T1.23): ok\n# {KW_TODO}(P.4) and {KW_TODO}(S05) and {KW_TODO}(S05a)\n"),
        ("a.txt", f"x # {KW_TODO}(T1.2)\n"),
    ],
)
def test_look_alikes_and_ids_do_not_fire(path: str, text: str) -> None:
    assert hy.todo_findings(path, text) == []


@pytest.mark.parametrize(
    "marker", [f"{KW_TODO}()", f"{KW_TODO}(t1.23)", f"{KW_TODO}(T1.23, T1.24)", f"{KW_TODO}[T1.23]"]
)
def test_malformed_ids_fail(marker: str) -> None:
    assert len(hy.todo_findings("a.py", f"# {marker}: x\n")) == 1


def test_fixme_with_id_is_rejected_as_specified() -> None:
    found = hy.todo_findings("a.py", f"# {KW_FIXME}(T1.23): x\n")
    assert len(found) == 1 and f"write {KW_TODO}(T<id>)" in found[0]


def test_marker_with_task_sub_id_passes() -> None:
    assert hy.todo_findings("a.py", f"# {KW_TODO}(T1.06a): codec split\n") == []


# --- (b) commit subjects ----------------------------------------------------------------------------------


@pytest.mark.parametrize("subject", ["T1.06a: split the codec", "T1.15e: admin list"])
def test_subject_with_task_sub_id_passes(subject: str) -> None:
    assert hy.subject_ok(subject)


@pytest.mark.parametrize("subject", ["", "T1.06ab: two letters", "fixup! T1.2: x", 'Revert "T1.2: x"', "T1.2 : x"])
def test_more_bad_subjects(subject: str) -> None:
    assert not hy.subject_ok(subject)


def _env(base: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOME=str(base),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CEILING_DIRECTORIES=str(base),
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=_env(repo.parent), check=True, capture_output=True)


def _commit(repo: Path, subject: str, name: str = "", text: str = "") -> None:
    if name:
        (repo / name).write_text(text, encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "--allow-empty", "--allow-empty-message", "-m", subject)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT), *args]
    return subprocess.run(argv, cwd=cwd, env=_env(cwd.parent), capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "docs").mkdir()
    _commit(root, "T1.1: init", "docs/glossary.md", GLOSSARY)
    _git(root, "tag", "base")
    return root


def _merge(repo: Path, branch: str, subjects: list[str], message: str | None) -> None:
    _git(repo, "checkout", "-q", "-b", branch, "base")
    for subject in subjects:
        _commit(repo, subject)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", branch, *(["-m", message] if message else ["--no-edit"]))


def test_range_with_merge_commits_checks_both_parents(repo: Path) -> None:
    _commit(repo, "T1.1: on main")
    _merge(repo, "side", ["T1.2: side ok", "fix side"], "merge: T1.2 side")
    _merge(repo, "other", ["docs: other ok"], None)  # default "Merge branch 'other'"
    done = _run(repo, "--commits", "base..main")
    assert done.returncode == 1
    assert "'fix side'" in done.stderr and "Merge branch 'other'" in done.stderr
    assert "side ok" not in done.stderr and "merge: T1.2" not in done.stderr
    assert "2 finding(s) (6 commits in base..main)" in done.stderr


def test_symmetric_range_and_empty_subject(repo: Path) -> None:
    _commit(repo, "")
    done = _run(repo, "--commits", "base...main")
    assert done.returncode == 1 and "subject ''" in done.stderr


def test_non_utf8_subject_does_not_crash(repo: Path) -> None:
    msg = repo.parent / "msg"
    msg.write_bytes(b"bad \xff\xfe subject\n")
    _git(repo, "commit", "-q", "--allow-empty", "-F", str(msg))
    done = _run(repo, "--commits", "base..main")
    assert done.returncode == 1 and "Traceback" not in done.stderr, done.stderr


@pytest.mark.parametrize("rng", ["HEAD..HEAD", "base..base", "^HEAD"])
def test_empty_ranges_fail_closed(repo: Path, rng: str) -> None:
    done = _run(repo, "--commits", rng)
    assert done.returncode == 2 and "no commits" in done.stderr, (done.stdout, done.stderr)


@pytest.mark.parametrize(
    "rng", ["", "HEAD..nosuch", "HEAD~5..HEAD", "a..b..c", "HEAD -- docs", "$(id)", "--since=1.day"]
)
def test_invalid_ranges_fail_closed(repo: Path, rng: str) -> None:
    done = _run(repo, f"--commits={rng}")
    assert done.returncode == 2, (done.stdout, done.stderr)


@pytest.mark.parametrize("rng", ["..HEAD", "HEAD..", "..", "...HEAD", "HEAD^{tree}"])
def test_range_with_an_empty_side_fails_closed(repo: Path, rng: str) -> None:
    _commit(repo, "fix stuff")
    done = _run(repo, f"--commits={rng}")
    assert done.returncode == 2, (done.stdout, done.stderr)


# --- (c) glossary -----------------------------------------------------------------------------------------


def test_duplicate_term_across_two_tables_and_markup() -> None:
    text = GLOSSARY + "\n## More\n\n| Term | Meaning |\n|---|---|\n| `App` | another meaning |\n"
    found = hy.glossary_findings(text)
    assert len(found) == 1 and "'app'" in found[0], found


def test_duplicate_definition_ignores_case_markup_and_period() -> None:
    found = hy.glossary_findings(GLOSSARY + "| **client app** | **The** `phone`  APP. | x |\n")
    assert len(found) == 1 and "same definition" in found[0], found


def test_real_glossary_is_clean() -> None:
    assert hy.glossary_findings((TOP / "docs" / "glossary.md").read_text(encoding="utf-8")) == []
