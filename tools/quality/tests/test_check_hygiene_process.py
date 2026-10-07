# SPDX-License-Identifier: Apache-2.0
"""Rule (d) of tools/quality/check_hygiene.py, review-process tags: QA wave/finding/round ids, review, QA and fix
round numbers, wave numbers, `round <n>` fixes/findings/xfails, lists or decisions of the team lead, and test file
names with a `_qa` suffix. Each tag fails in any case; look-alikes in ordinary prose and code pass.

Planted tags are assembled at run time (`QA`, `ROUND`, `FIX`, `WAVE`, `LEAD`, ...) so this file holds none.
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
    spec = importlib.util.spec_from_file_location("check_hygiene_process_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hy = _load()
QA = "Q" + "A"
QA_L = QA.lower()
ROUND = "rou" + "nd"
FIX = "fi" + "x"
WAVE = "wa" + "ve"
LEAD = "le" + "ad"
REVIEW = "re" + "view"
NOQA = "no" + "qa"

PLANTED = [
    f"{QA}-W7c-r3 finding: the widget shows a stale value",
    f"see {QA_L}-w7a",
    f"{QA_L}_w7c fixtures",
    f"# {QA} S99: example.com is the only allowed host",
    f"{QA} {ROUND} 2 for the parser",
    f"{QA_L} {ROUND} 2: x",
    f"{REVIEW}/{QA} {ROUND} 1",
    f"{REVIEW} {ROUND} 1",
    f"{FIX} {ROUND} 1: empty input is rejected",
    f"parser {FIX} {ROUND}s 1 and 2",
    f"{FIX}_{ROUND}1",
    f"{FIX}_r1",
    f"{QA_L}_r2",
    f'reason="{QA} r2: gap"',
    f"{WAVE.title()} 7c {ROUND} 1",
    f"{WAVE}7a",
    f"attacks on the {ROUND}-1 fixes",
    f"timestamps ({ROUND}-1 xfails)",
    f"{ROUND} 2 findings",
    f"exactly the {LEAD}'s list",
    f"exactly the {LEAD}\u2019s list",
    f"a {LEAD} decision",
]


@pytest.mark.parametrize("line", PLANTED + [line.upper() for line in PLANTED])
def test_process_tag_fails(line: str) -> None:
    findings = hy.private_ref_findings("tests/t.py", f"ok\n{line}\n")
    assert len(findings) == 1, findings
    assert findings[0].startswith("tests/t.py:2: private reference "), findings


@pytest.mark.parametrize("name", [f"test_{ROUND}_2_{FIX}es", f"the_{LEAD}_list"])
def test_underscore_joined_round_and_lead_tags_fail(name: str) -> None:
    findings = hy.private_ref_findings("tests/t.py", f"def {name}() -> None: ...\n")
    assert len(findings) == 1 and findings[0].startswith("tests/t.py:1: private reference "), findings


@pytest.mark.parametrize(
    "line",
    [
        f"values {ROUND} 2 decimals; a {ROUND}-trip; {ROUND}ed to 2 places; {ROUND}(2.5)",
        f"a {ROUND} 2 of the protocol handshake",
        f"prefix_r1 and pre{FIX} {ROUND} 1 are not tags",
        f"x = 1  # {NOQA}: S603 - fixed argv",
        f"aqua w2 colour, a {QA_L}: x line, {QA_L}-tests",
        f"a sine {WAVE} 440 Hz, micro{WAVE} 2, {WAVE}form 2",
        f"{LEAD}ing decisions, mis{LEAD + 's'} list, the {LEAD} engineer",
        f"{REVIEW} rating 5, {FIX}ture r1",
    ],
)
def test_look_alike_passes(line: str) -> None:
    assert hy.private_ref_findings("docs/a.md", line + "\n") == []


@pytest.mark.parametrize(
    "path",
    [
        f"tests/test_widget_{QA_L}.py",
        f"tests/test_widget_{QA_L}2.py",
        f"tools/tests/test_example-{QA_L.upper()}.sh",
        f"tests/test_{FIX}_{ROUND}1.py",
        f"tests/test_widget_{QA_L}_w7c_r3.py",
        f"tests/test_widget_{FIX}_r1_deps.py",
        f"examples/{WAVE}-7a/x.json",
    ],
)
def test_process_tag_in_file_name_fails(path: str) -> None:
    findings = hy.private_name_findings(path)
    assert findings and all(f.startswith(f"{path}: private reference ") for f in findings), findings


@pytest.mark.parametrize(
    "path",
    [f"docs/{QA_L}.md", f"tools/{QA_L}/test_a.py", f"tests/test_{QA_L}_helpers.py", "tests/test_squad.py"],
)
def test_ordinary_file_name_passes(path: str) -> None:
    assert hy.private_name_findings(path) == []


def _git(repo: Path, *args: str) -> None:
    env = dict(
        os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CEILING_DIRECTORIES=str(repo.parent)
    )
    subprocess.run(["git", *args], cwd=repo, env=env, check=True)


def test_cli_flags_process_tags_in_text_and_names(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "glossary.md").write_text(
        "# G\n\n| Term | Meaning |\n|---|---|\n| **a** | b |\n", encoding="utf-8"
    )
    (repo / "test_example_qa2.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "notes.md").write_text(f"Plain.\nSee {QA} {ROUND} 2.\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    done = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True, check=False)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "test_example_qa2.py: private reference '_qa2.py' in the path name" in done.stderr
    assert "notes.md:2: private reference" in done.stderr
