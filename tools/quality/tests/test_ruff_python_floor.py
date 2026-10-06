# SPDX-License-Identifier: Apache-2.0
"""Python floor of the ruff gate (T1.133, ENGINEERING.md section 8): code must parse on Python 3.11.

The plugin runs inside the Hermes venv, and Hermes supports Python 3.11 and newer; the dev venv is newer. With
`target-version = "py311"` ruff reports syntax added after 3.11 as `invalid-syntax`, and `ruff format` keeps the
parentheses of `except (A, B):` (with py314 it removed them: PEP 758 syntax that 3.11 cannot compile). The floor
holds for every tracked .py, also outside the lint dirs and in generated code: a syntax-only ruff run that reads no
config (`--isolated`, every rule off) over `git ls-files '*.py'`. Planted code goes through stdin or a tmp file.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from test_ruff_baseline import CORE_FILE, ROOT, lint_stdin, ruff

FLOOR = "py311"
TOOLS_FILE = "tools/quality/_planted.py"
PLUGIN_TEST_FILE = "hermes-plugin/tests/test_planted.py"
NEWER_SYNTAX = [
    "try:\n    pass\nexcept ValueError, TypeError:\n    pass\n",  # 3.14 (PEP 758)
    "type Alias = int\n",  # 3.12 (PEP 695)
    "def first[T](items: list[T]) -> T:\n    return items[0]\n",  # 3.12 (PEP 695)
    'def key(data: dict[str, str]) -> str:\n    return f"{data["k"]}"\n',  # 3.12 (PEP 701)
]
PARENTHESISED = (
    "def parse(text):\n    try:\n        return int(text)\n    except (ValueError, TypeError):\n        return 0\n"
)


@pytest.mark.parametrize("filename", [CORE_FILE, TOOLS_FILE, PLUGIN_TEST_FILE])
@pytest.mark.parametrize("source", NEWER_SYNTAX)
def test_syntax_newer_than_the_floor_fails(source: str, filename: str) -> None:
    done = lint_stdin(source, filename)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "invalid-syntax" in done.stdout and "Python 3.11" in done.stdout, done.stdout


def test_parenthesised_except_passes_lint_and_format_keeps_it() -> None:
    assert lint_stdin(PARENTHESISED).returncode == 0
    done = ruff("format", "--no-cache", "--stdin-filename", CORE_FILE, "-", stdin=PARENTHESISED)
    assert done.returncode == 0 and done.stdout == PARENTHESISED, done.stdout + done.stderr


def syntax_check(paths: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Syntax errors of `paths` for the floor only: no config file is read, every lint rule is off."""
    argv = [sys.executable, "-m", "ruff", "check", "--no-cache", "--isolated", "--target-version", FLOOR]
    argv += ["--ignore", "ALL", "--output-format", "concise", *paths]
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def tracked_python_files() -> list[str]:
    """Every tracked *.py of the repository (git failing raises: the check never passes on an empty list)."""
    done = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [name for name in done.stdout.split("\0") if name]


def test_every_tracked_python_file_parses_on_the_floor() -> None:
    files = tracked_python_files()
    assert any(name.startswith("hermes-plugin/glasses/generated/") for name in files), files
    assert "tools/quality/check_core_purity.py" in files, files
    done = syntax_check(files, ROOT)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("source", NEWER_SYNTAX)
def test_the_syntax_check_fails_on_newer_syntax_in_any_file(tmp_path: Path, source: str) -> None:
    (tmp_path / "planted.py").write_text(source, encoding="utf-8")
    done = syntax_check(["planted.py"], tmp_path)
    assert done.returncode == 1 and "invalid-syntax" in done.stdout and "Python 3.11" in done.stdout, done.stdout
