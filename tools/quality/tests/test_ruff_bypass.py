# SPDX-License-Identifier: Apache-2.0
"""Ruff gate (T1.97): code in the tree cannot switch the gate off.

- A file-level ``ruff: noqa`` comment (blanket or with codes) is refused: blanket ones fail with PGH004, ones with
  codes are a per-file ignore without a task id, which only the ruff.toml baseline may hold.
- A blanket line ``noqa`` fails with PGH004, a dead one with RUF100, and one without a reason after its codes is
  refused here (ENGINEERING.md section 5). The one reason form: ``noqa: CODE[, CODE] - reason`` (see ruff.toml).
- A nested ruff config (``ruff.toml``, ``.ruff.toml``, ``[tool.ruff]`` in a ``pyproject.toml``) is refused, and the gate
  passes ``--config ruff.toml`` so one would not apply anyway.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tokenize
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LINT_DIRS = ("hermes-plugin", "protocol", "tools")
GATE_CHECK = ("check", "--no-cache", "--config", "ruff.toml", "--output-format", "concise")
FILE_EXEMPTION_RE = re.compile(r"#\s*(?:ruff|flake8)\s*:\s*noqa", re.IGNORECASE)
# A line noqa (anywhere in a comment, as ruff reads it), its codes, then the rest of the comment.
LINE_NOQA_RE = re.compile(r"#\s*noqa\s*:\s*[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*(?P<rest>.*)", re.IGNORECASE)
REASON_RE = re.compile(r"\s+-\s+\S")
CONFIG_NAMES = {"ruff.toml", ".ruff.toml", "pyproject.toml"}
FIVE_PARAMS = "def f(a, b, c, d, e):\n    return a + b + c + d + e\n"
# Built from parts so that this file holds no such comment itself.
FILE_NOQA = "# ruff" + ": noqa"
LINE_NOQA = "# no" + "qa: PLR0913, PLR0917"


def file_exemptions(source: str) -> list[int]:
    """Line numbers of file-level exemption comments (comments only, as ruff reads them; strings do not count)."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return [tok.start[0] for tok in tokens if tok.type == tokenize.COMMENT and FILE_EXEMPTION_RE.search(tok.string)]


def reasonless_noqa(source: str) -> list[int]:
    """Line numbers of line ``noqa`` comments with no ``- reason`` after their codes (comments only)."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    found = []
    for tok in tokens:
        hit = LINE_NOQA_RE.search(tok.string) if tok.type == tokenize.COMMENT else None
        if hit is not None and not REASON_RE.match(hit["rest"]):
            found.append(tok.start[0])
    return found


def _read_in_tree(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def nested_configs(paths: list[str], read: Callable[[str], str] = _read_in_tree) -> list[str]:
    """Every ruff config except the root ruff.toml (a pyproject.toml only when it has a [tool.ruff] table)."""
    found = []
    for path in paths:
        name = Path(path).name
        if name not in CONFIG_NAMES or path == "ruff.toml":
            continue
        if name == "pyproject.toml" and "ruff" not in tomllib.loads(read(path)).get("tool", {}):
            continue
        found.append(path)
    return found


def _tree_files(*pathspec: str) -> list[str]:
    argv = ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "--", *pathspec]
    done = subprocess.run(argv, capture_output=True, text=True, check=True)
    return done.stdout.splitlines()


def _check(source: str, filename: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "ruff", *GATE_CHECK, "--stdin-filename", filename, "-"]
    return subprocess.run(argv, cwd=ROOT, input=source, capture_output=True, text=True, check=False)


# --- file-level exemptions ---------------------------------------------------------------------------------------


def test_no_file_level_noqa_in_the_tree() -> None:
    files = [path for path in _tree_files(*LINT_DIRS) if path.endswith(".py")]
    assert len(files) > 20, f"scanned only {len(files)} files: the tree listing did not apply"
    found = [f"{path}:{line}" for path in files for line in file_exemptions((ROOT / path).read_text(encoding="utf-8"))]
    assert found == [], "file-level noqa: fix the code, or add a ruff.toml baseline entry with a task id"


@pytest.mark.parametrize(
    "comment", [FILE_NOQA, FILE_NOQA + ": PLR0913, PLR0917", "#Ruff:NOQA:C901", "# flake8" + ": noqa", "#flake8:noqa"]
)
def test_file_level_noqa_is_found(comment: str) -> None:
    assert file_exemptions(f'"""Doc."""\n\n{comment}\n{FIVE_PARAMS}') == [3]


def test_file_level_noqa_after_other_comment_text_is_found() -> None:
    assert file_exemptions(f"X = 1\n# header text  {FILE_NOQA}: C901\n") == [2]


def test_file_level_noqa_in_a_string_is_not_a_directive() -> None:
    assert file_exemptions(f"TEXT = {FILE_NOQA!r}\n") == []


def test_file_level_noqa_with_codes_is_a_bypass_ruff_itself_lets_through() -> None:
    # Why the scan above exists: ruff honours this comment and reports nothing.
    assert _check(f"{FILE_NOQA}: PLR0913, PLR0917\n{FIVE_PARAMS}", "hermes-plugin/glasses/_planted.py").returncode == 0


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (f"{FILE_NOQA}\n{FIVE_PARAMS}", "PGH004"),
        ("def f(a, b, c, d, e):  # noqa\n    return a + b + c + d + e\n", "PGH004"),
        ("def f(a, b, c, d, e):  # NOQA - later\n    return a + b + c + d + e\n", "PGH004"),
        ("x = 1  # noqa: E501\n", "RUF100"),
        ("import subprocess  # noqa: S404\n\nsubprocess.run([])\n", "RUF100"),
    ],
    ids=["file-blanket", "line-blanket", "line-blanket-reason", "dead-code", "non-enabled-code"],
)
def test_blanket_or_dead_noqa_fails(source: str, code: str) -> None:
    done = _check(source, "hermes-plugin/glasses/_planted.py")
    assert done.returncode == 1 and f" {code} " in done.stdout, done.stdout + done.stderr


# --- reasons ----------------------------------------------------------------------------------------------------


def test_every_noqa_in_the_tree_has_a_reason() -> None:
    files = [path for path in _tree_files(*LINT_DIRS) if path.endswith(".py")]
    assert len(files) > 20, f"scanned only {len(files)} files: the tree listing did not apply"
    found = [f"{path}:{line}" for path in files for line in reasonless_noqa((ROOT / path).read_text(encoding="utf-8"))]
    assert found == [], "a noqa needs a reason after its codes: `# noqa: CODE - reason` (ENGINEERING.md section 5)"


@pytest.mark.parametrize(
    "comment",
    [
        LINE_NOQA,
        LINE_NOQA + "  ",
        LINE_NOQA + " -",
        LINE_NOQA + " - ",
        LINE_NOQA + " -- double dash",
        LINE_NOQA + "  # hash form",
        LINE_NOQA + "  (parenthesised)",
        LINE_NOQA + "-glued",
        "#NOQA:PLR0913,PLR0917",
        "# see docs  " + LINE_NOQA,
    ],
)
def test_noqa_without_a_reason_is_found(comment: str) -> None:
    assert reasonless_noqa(f"def f(a, b, c, d, e):  {comment}\n    return a\n") == [1]


@pytest.mark.parametrize(
    "comment", [LINE_NOQA + " - mirrors the API", "# no" + "qa:E501,W291 - long URL", "# no" + "qa: E501 W291 - long"]
)
def test_noqa_with_a_reason_passes(comment: str) -> None:
    assert reasonless_noqa(f"x = 1  {comment}\n") == []


def test_noqa_in_a_string_or_file_level_is_not_a_line_noqa() -> None:
    assert reasonless_noqa(f"TEXT = {LINE_NOQA!r}\n{FILE_NOQA}: C901\n") == []


def test_the_reason_form_is_one_ruff_honours() -> None:
    assert (
        _check(f"def f(a, b, c, d, e):  {LINE_NOQA} - why\n    return a\n", "hermes-plugin/glasses/_p.py").returncode
        == 0
    )


# --- nested configs ----------------------------------------------------------------------------------------------


def test_no_nested_ruff_config_in_the_tree() -> None:
    assert nested_configs(_tree_files()) == [], "one ruff config only: the root ruff.toml"


def test_nested_configs_are_found() -> None:
    sources = {"hermes-plugin/pyproject.toml": "[tool.ruff.lint]\nselect = ['F']\n", "a/pyproject.toml": "[x]\n"}
    paths = ["ruff.toml", "tools/zz/ruff.toml", "protocol/.ruff.toml", *sources]
    assert nested_configs(paths, sources.__getitem__) == [
        "tools/zz/ruff.toml",
        "protocol/.ruff.toml",
        "hermes-plugin/pyproject.toml",
    ]


def test_every_ruff_call_of_the_gate_names_the_root_config() -> None:
    calls = [line for line in (ROOT / "mk/lint-py.mk").read_text(encoding="utf-8").splitlines() if "-m ruff" in line]
    assert len(calls) == 2 and all("--config ruff.toml" in line for line in calls), calls


def _plant_tree(root: Path) -> None:
    shutil.copy(ROOT / "ruff.toml", root / "ruff.toml")
    for sub, name, text in (
        ("tools/zz", "ruff.toml", "[lint]\nselect = ['F']\n"),
        ("hermes-plugin", "pyproject.toml", "[project]\nname = 'x'\n\n[tool.ruff.lint]\nselect = ['F']\n"),
    ):
        (root / sub).mkdir(parents=True)
        (root / sub / name).write_text(text, encoding="utf-8")
        (root / sub / "planted.py").write_text(FIVE_PARAMS, encoding="utf-8")


def test_a_nested_config_cannot_override_the_root_config(tmp_path: Path) -> None:
    _plant_tree(tmp_path)
    run = [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "concise", "tools", "hermes-plugin"]
    without = subprocess.run(run, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert without.returncode == 0, "plant is not a bypass without --config: " + without.stdout
    gate = [sys.executable, "-m", "ruff", *GATE_CHECK, "tools", "hermes-plugin"]
    done = subprocess.run(gate, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert done.returncode == 1, done.stdout + done.stderr
    for path in ("tools/zz/planted.py", "hermes-plugin/planted.py"):
        assert f"{path}:1:5: PLR0913 " in done.stdout, done.stdout
