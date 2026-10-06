# SPDX-License-Identifier: Apache-2.0
"""ruff gate scope: every lint dir is covered, tests keep the nesting limit, generated code is out.

Planted code goes through stdin with a ``--stdin-filename`` inside the tree, so nothing is written and the real
``ruff.toml`` (including ``force-exclude``) decides.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CC11 = "def f(a):\n" + "".join(f"    if a == {n}:\n        return {n}\n" for n in range(10)) + "    return -1\n"
NEST4 = (
    "def f(xs):\n    for x in xs:\n        if x:\n            while x:\n"
    "                if x > 1:\n                    xs.append(1)\n    return xs\n"
)


def _ruff(*args: str, stdin: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "ruff", *args[:1], "--config", "ruff.toml", *args[1:]]
    return subprocess.run(argv, cwd=ROOT, input=stdin, capture_output=True, text=True, check=False)


def _check(source: str, filename: str) -> subprocess.CompletedProcess[str]:
    return _ruff("check", "--no-cache", "--output-format", "concise", "--stdin-filename", filename, "-", stdin=source)


@pytest.mark.parametrize(
    "filename",
    [
        "hermes-plugin/glasses/qa_planted.py",
        "protocol/qa_planted.py",
        "tools/quality/qa_planted.py",
        "tools/codegen/qa_planted.py",
        "tools/mock-llm/qa_planted.py",
        "tools/test-hermes/glasses_min/qa_planted.py",
    ],
)
def test_every_lint_dir_reports_complexity(filename: str) -> None:
    done = _check(CC11, filename)
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"{filename}:" in done.stdout and " C901 " in done.stdout


def test_print_in_a_tool_fails_with_t201() -> None:
    done = _check("print(1)\n", "tools/mock-llm/qa_planted.py")
    assert done.returncode == 1 and " T201 " in done.stdout, done.stdout


def test_tests_keep_the_nesting_limit() -> None:
    done = _check(NEST4, "hermes-plugin/tests/test_planted_nesting.py")
    assert done.returncode == 1 and " PLR1702 " in done.stdout, done.stdout


def test_generated_dir_is_excluded_from_lint() -> None:
    done = _check(CC11, "hermes-plugin/glasses/generated/qa_planted.py")
    assert done.returncode == 0, done.stdout
    assert " C901 " not in done.stdout


@pytest.mark.parametrize("filename", ["hermes-plugin/glasses/qa_planted.py", "tools/codegen/qa_planted.py"])
def test_unformatted_code_fails_format_check(filename: str) -> None:
    done = _ruff("format", "--no-cache", "--check", "--stdin-filename", filename, "-", stdin="X=1\n")
    assert done.returncode == 1, done.stdout + done.stderr
