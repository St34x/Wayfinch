# SPDX-License-Identifier: Apache-2.0
"""Tests of tools/codegen/check_fresh.sh (T1.104): the whole generated directory is compared with `diff -r`.

Every case copies hermes-plugin/glasses/generated into a scratch dir, damages the copy and runs
``sh check_fresh.sh <this interpreter> <copy>`` the way `make check-codegen` runs it on the real directory.
"""

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

CODEGEN = Path(__file__).resolve().parents[1]
ROOT = CODEGEN.parents[1]
GENERATED = ROOT / "hermes-plugin/glasses/generated"
SCRIPT = CODEGEN / "check_fresh.sh"
Damage = Callable[[Path], object]

# Repro: a hand-written module in generated/ that lint (excluded) and the size gate (exempt) never see.
HAND_WRITTEN = """# SPDX-License-Identifier: Apache-2.0
def helper(a, b, c, d, e):
    print(a, b, c, d, e)
"""


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def _check(*args: str) -> subprocess.CompletedProcess[str]:
    argv = ["sh", str(SCRIPT), *args]
    return subprocess.run(argv, capture_output=True, text=True, check=False, cwd=ROOT)


def _copy(tmp_path: Path) -> Path:
    copy = tmp_path / "generated"
    shutil.copytree(GENERATED, copy, ignore=shutil.ignore_patterns("__pycache__"))
    return copy


def test_real_generated_directory_is_fresh() -> None:
    done = _check(sys.executable)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "matches the generator (2 files)" in done.stdout


def test_untouched_copy_and_interpreter_cache_pass(tmp_path: Path) -> None:
    copy = _copy(tmp_path)
    (copy / "__pycache__").mkdir()
    (copy / "__pycache__" / "protocol_types.cpython-314.pyc").write_bytes(b"\0")
    done = _check(sys.executable, str(copy))
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        (lambda d: (d / "helpers.py").write_text(HAND_WRITTEN, encoding="utf-8"), "Only in {copy}: helpers.py"),
        (lambda d: (d / "notes.txt").write_text("x\n", encoding="utf-8"), "Only in {copy}: notes.txt"),
        (lambda d: (d / "sub").mkdir() or (d / "sub/x.py").write_text("", encoding="utf-8"), "Only in {copy}: sub"),
        (lambda d: (d / "__init__.py").unlink(), "__init__.py"),
        (lambda d: (d / "protocol_types.py").unlink(), "protocol_types.py"),
        (lambda d: _append(d / "protocol_types.py", "\n"), "+++"),
        (lambda d: _append(d / "__init__.py", "x = 1\n"), "-x = 1"),
    ],
    ids=["hand-written", "extra-file", "extra-subdir", "missing-init", "missing-module", "edited", "edited-init"],
)
def test_extra_missing_or_edited_file_fails(tmp_path: Path, damage: Damage, expected: str) -> None:
    copy = _copy(tmp_path)
    damage(copy)
    done = _check(sys.executable, str(copy))
    assert done.returncode == 1, done.stdout + done.stderr
    assert expected.format(copy=copy) in done.stdout
    assert f"check-codegen: {copy} is stale" in done.stderr


def test_missing_directory_fails(tmp_path: Path) -> None:
    done = _check(sys.executable, str(tmp_path / "absent"))
    assert done.returncode == 1
    assert "is missing" in done.stderr


def test_generator_failure_and_bad_usage_exit_2(tmp_path: Path) -> None:
    assert _check(str(tmp_path / "no-python"), str(_copy(tmp_path))).returncode == 2
    assert _check().returncode == 2
    assert _check("", str(tmp_path)).returncode == 2
