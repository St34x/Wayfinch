# SPDX-License-Identifier: Apache-2.0
"""mypy gate: planted errors in every scope corner, missing config, strict pinned.

Each case runs the real mypy of this venv with the real ``mypy.ini`` on a scratch copy of the checked tree.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UNTYPED = '# SPDX-License-Identifier: Apache-2.0\n"""Planted probe."""\n\n\ndef f(x):\n    return x\n'


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    skip = shutil.ignore_patterns("__pycache__", ".mypy_cache")
    shutil.copytree(ROOT / "hermes-plugin" / "glasses", tmp_path / "hermes-plugin" / "glasses", ignore=skip)
    shutil.copytree(ROOT / "tools", tmp_path / "tools", ignore=skip)
    shutil.copy(ROOT / "mypy.ini", tmp_path / "mypy.ini")
    return tmp_path


@pytest.fixture(scope="module")
def cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("mypy-scope-cache")


def _mypy(root: Path, cache: Path) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "mypy", "--config-file", "mypy.ini", "--cache-dir", str(cache)]
    return subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    "where",
    [
        "tools/mock-llm/qa_untyped_mock.py",
        "tools/test-hermes/glasses_min/qa_untyped_min.py",
        "hermes-plugin/glasses/generated/qa_untyped_gen.py",
    ],
)
def test_untyped_function_in_scope_corner_fails(tree: Path, cache: Path, where: str) -> None:
    target = tree / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(UNTYPED, encoding="utf-8")
    done = _mypy(tree, cache)
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"{where}:5: error: Function is missing a type annotation" in done.stdout


def test_missing_config_fails(tree: Path, cache: Path) -> None:
    (tree / "mypy.ini").unlink()
    assert _mypy(tree, cache).returncode != 0


def test_config_keeps_strict_and_the_full_scope() -> None:
    parser = configparser.ConfigParser()
    parser.read(ROOT / "mypy.ini", encoding="utf-8")
    assert parser.getboolean("mypy", "strict")
    assert [part.strip() for part in parser.get("mypy", "files").split(",")] == ["hermes-plugin/glasses", "tools"]
