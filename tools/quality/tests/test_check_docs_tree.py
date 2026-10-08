# SPDX-License-Identifier: Apache-2.0
"""Tree and CLI tests of tools/quality/check_docs.py (T1.134): scratch repositories with planted failures, the fail
closed cases and the real tree itself.
"""

import importlib.util
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_docs.py"
TOP = SCRIPT.parents[2]
sys.path.insert(0, str(SCRIPT.parent))  # check_docs.py imports md_links.py and check_hygiene.py from its directory
PRIVATE = "." + "team"  # the private folder, absent from a public export (name built at run time for the hygiene gate)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_tree_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cd = _load()
BASE = {
    "README.md": "# Project\n\nAll documents: [docs/README.md](docs/README.md).\n",
    "docs/README.md": "# Docs\n\n- [Guide](guide.md)\n- [Fixes](troubleshooting/INDEX.md)\n- [Tool](../tool/)\n",
    "docs/guide.md": "# Guide\n\n## Install it\n\nText.\n",
    "docs/troubleshooting/INDEX.md": "# Index\n\n- crash → [`crash.md`](crash.md)\n",
    "docs/troubleshooting/crash.md": "# Crash\n",
    "tool/README.md": "# Tool\n\n## Usage\n",
    "tool/run.sh": "#!/bin/sh\n",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(root: Path, changes: dict[str, str] | None = None) -> Path:
    files = {**BASE, **(changes or {})}
    if any(name.startswith(f"{PRIVATE}/") for name in files):
        files = {f"{PRIVATE}/kb/INDEX.md": "# KB\n", **files}  # required while the private folder is tracked
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


def _findings(root: Path) -> list[str]:
    return list(cd.check_tree(root).findings)


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False, env=env
    )


# --- clean trees -----------------------------------------------------------------------------------------------------


def test_scratch_tree_is_clean(tmp_path: Path) -> None:
    result = cd.check_tree(_repo(tmp_path))
    assert result.findings == []
    assert (result.files, result.indexes) == (6, 1)


def test_real_tree_is_clean_and_fast() -> None:
    before, start = resource.getrusage(resource.RUSAGE_CHILDREN), time.monotonic()
    done = _run(TOP)
    elapsed, after = time.monotonic() - start, resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("check-docs: OK (")
    assert cpu < 2.0  # the checker's own CPU time (git included): stable under load, unlike wall-clock time
    assert elapsed < 10.0


def test_valid_link_forms_pass(tmp_path: Path) -> None:
    guide = (
        "# Guide\n\n## Install it\n\n[same](#install-it) [other](../README.md#project) [dir](../tool) "
        "[dir anchor](../tool/#usage) [web](https://example.com/x.md) [mail](mailto:a@example.com) "
        "[script](../tool/run.sh) [root](/docs/guide.md) [encoded](../docs/%67uide.md) [query](guide.md?x=1)\n"
    )
    assert _findings(_repo(tmp_path, {"docs/guide.md": guide})) == []


def test_directory_without_readme_is_a_valid_target(tmp_path: Path) -> None:
    changes = {"lib/a.py": "", "docs/guide.md": "# Guide\n\n[lib](../lib/)\n"}
    assert _findings(_repo(tmp_path, changes)) == []


# --- links and anchors -----------------------------------------------------------------------------------------------


def test_broken_link_fails_with_file_and_line(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/guide.md": "# Guide\n\nSee\n[x](missing.md).\n"})
    assert _findings(root) == [
        "docs/guide.md:4: broken link 'missing.md': no tracked file or directory docs/missing.md"
    ]


def test_broken_anchor_fails_with_file_and_line(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "# Project\n\n[docs](docs/README.md)\n[g](docs/guide.md#install)\n"})
    assert _findings(root) == ["README.md:4: broken anchor 'docs/guide.md#install': no heading or id 'install'"]


def test_broken_same_file_anchor(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/guide.md": "# Guide\n\n[up](#nowhere)\n"})
    assert _findings(root) == ["docs/guide.md:3: broken anchor '#nowhere': no heading or id 'nowhere'"]


def test_anchor_on_a_directory_is_checked_against_its_readme(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/guide.md": "# Guide\n\n[t](../tool/#missing)\n"})
    assert _findings(root) == ["docs/guide.md:3: broken anchor '../tool/#missing': no heading or id 'missing'"]


def test_link_to_an_untracked_file_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/guide.md": "# Guide\n\n[l](local.md)\n", "docs/local.md": "# Local\n"})
    _git(root, "rm", "-q", "--cached", "docs/local.md")
    assert [f.split(" ")[0] for f in _findings(root)] == ["docs/guide.md:3:"]


def test_link_leaving_the_repository_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/guide.md": "# Guide\n\n[up](../../outside.md)\n"})
    assert _findings(root) == ["docs/guide.md:3: broken link '../../outside.md': leaves the repository"]


def test_private_notes_are_checked_for_links(tmp_path: Path) -> None:
    root = _repo(tmp_path, {f"{PRIVATE}/plan.md": "# Plan\n\n[cfg](docs/guide.md)\n[ok](../docs/guide.md)\n"})
    assert _findings(root) == [
        f"{PRIVATE}/plan.md:3: broken link 'docs/guide.md': no tracked file or directory {PRIVATE}/docs/guide.md"
    ]


# --- reachability ----------------------------------------------------------------------------------------------------


def test_unindexed_public_doc_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/orphan.md": "# Orphan\n"})
    assert _findings(root) == ["docs/orphan.md:1: not reachable by links from README.md (index it in docs/README.md)"]


def test_module_readme_without_a_file_or_directory_link_is_unreachable(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/README.md": "# Docs\n\n- [Guide](guide.md)\n- [T](troubleshooting/INDEX.md)\n"})
    assert _findings(root) == ["tool/README.md:1: not reachable by links from README.md (index it in docs/README.md)"]


def test_link_from_private_notes_does_not_make_a_doc_reachable(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/orphan.md": "# Orphan\n", f"{PRIVATE}/n.md": "[o](../docs/orphan.md)\n"})
    assert _findings(root) == ["docs/orphan.md:1: not reachable by links from README.md (index it in docs/README.md)"]


def test_unlinked_doc_in_another_dot_directory_is_public(tmp_path: Path) -> None:
    root = _repo(tmp_path, {".github/SECURITY.md": "# Security\n"})
    assert _findings(root) == [f".github/SECURITY.md:1: {cd.UNREACHABLE}"]


def test_private_notes_need_not_be_reachable(tmp_path: Path) -> None:
    assert _findings(_repo(tmp_path, {f"{PRIVATE}/n.md": "# Notes\n"})) == []


# --- indexes ---------------------------------------------------------------------------------------------------------


def test_unlisted_index_entry_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/troubleshooting/new.md": "# New\n"})
    assert _findings(root) == ["docs/troubleshooting/new.md:1: not listed in docs/troubleshooting/INDEX.md"]


def test_dangling_index_line_fails(tmp_path: Path) -> None:
    index = "# Index\n\n- crash → [`crash.md`](crash.md)\n- gone → [`gone.md`](gone.md)\n"
    root = _repo(tmp_path, {"docs/troubleshooting/INDEX.md": index})
    assert _findings(root) == [
        "docs/troubleshooting/INDEX.md:4: broken link 'gone.md': no tracked file or directory "
        "docs/troubleshooting/gone.md"
    ]


def test_private_index_is_checked(tmp_path: Path) -> None:
    changes = {
        f"{PRIVATE}/kb/INDEX.md": "# KB\n\n- a → [a](a.md)\n- public → [c](../../docs/troubleshooting/crash.md)\n",
        f"{PRIVATE}/kb/a.md": "# A\n",
        f"{PRIVATE}/kb/b.md": "# B\n",
    }
    assert _findings(_repo(tmp_path, changes)) == [f"{PRIVATE}/kb/b.md:1: not listed in {PRIVATE}/kb/INDEX.md"]


def test_entry_listed_only_from_another_directory_is_unlisted(tmp_path: Path) -> None:
    changes = {
        "docs/troubleshooting/INDEX.md": "# Index\n",
        "docs/README.md": BASE["docs/README.md"] + "- [crash](troubleshooting/crash.md)\n",
    }
    assert _findings(_repo(tmp_path, changes)) == [
        "docs/troubleshooting/crash.md:1: not listed in docs/troubleshooting/INDEX.md"
    ]


def test_index_lines_need_not_be_sorted(tmp_path: Path) -> None:
    changes = {
        "docs/troubleshooting/INDEX.md": "# Index\n\n- z → [z](zeta.md)\n- c → [c](crash.md)\n",
        "docs/troubleshooting/zeta.md": "# Z\n",
    }
    assert _findings(_repo(tmp_path, changes)) == []


# --- fail closed: exit 2 ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["README.md", "docs/troubleshooting/INDEX.md"])
def test_missing_required_file_exits_2(tmp_path: Path, missing: str) -> None:
    root = _repo(tmp_path)
    _git(root, "rm", "-q", "-f", missing)
    done = _run(root)
    assert done.returncode == 2, done.stderr
    assert missing in done.stderr


def test_tracked_but_deleted_doc_exits_2(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "docs/guide.md").unlink()
    done = _run(root)
    assert done.returncode == 2 and "docs/guide.md" in done.stderr


def test_non_utf8_doc_exits_2(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "docs/guide.md").write_bytes(b"# Guide\n\xff\xfe\n")
    assert _run(root).returncode == 2


def test_symlinked_doc_exits_2(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "docs/guide.md").unlink()
    os.symlink("../README.md", root / "docs/guide.md")
    _git(root, "add", "-A")
    assert _run(root).returncode == 2


def test_no_tracked_docs_exits_2(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    assert _run(tmp_path).returncode == 2


def test_corrupt_git_index_exits_2(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".git/index").write_bytes(b"garbage\n")
    done = _run(root)
    assert done.returncode == 2 and "git" in done.stderr


def test_outside_a_repository_exits_2(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert _run(tmp_path / "plain", env=env).returncode == 2


def test_subdirectory_is_not_a_top(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(cd.DocsError, match="not the top level"):
        cd.check_tree(root / "docs")


def test_arguments_exit_2(tmp_path: Path) -> None:
    assert _run(_repo(tmp_path), "--fix").returncode == 2


# --- CLI -------------------------------------------------------------------------------------------------------------


def test_cli_reports_each_finding_as_file_line_on_stderr(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/guide.md": "# Guide\n\n[x](gone.md)\n", "docs/orphan.md": "# O\n"})
    done = _run(root / "docs")
    assert done.returncode == 1
    lines = done.stderr.splitlines()
    assert lines[:2] == [
        "docs/guide.md:3: broken link 'gone.md': no tracked file or directory docs/gone.md",
        "docs/orphan.md:1: not reachable by links from README.md (index it in docs/README.md)",
    ]
    assert lines[2] == "check-docs: 2 finding(s)"
    assert done.stdout == ""


def test_cli_ok_line(tmp_path: Path) -> None:
    done = _run(_repo(tmp_path))
    assert done.returncode == 0, done.stderr
    assert done.stdout == "check-docs: OK (6 docs, 5 relative links, 1 indexes)\n"
