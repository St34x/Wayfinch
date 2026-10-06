# SPDX-License-Identifier: Apache-2.0
"""Tree edge cases of tools/codegen/check_fresh.sh: symlinks, empty dirs and the __pycache__ exclusion.

Every case copies hermes-plugin/glasses/generated into a scratch dir, changes the copy and runs
``sh check_fresh.sh <this interpreter> <copy>``. The strict xfail pins a known gap.
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
HAND_WRITTEN = "def helper(a, b, c, d, e):\n    print(a, b, c, d, e)\n"


def _run(copy: Path) -> subprocess.CompletedProcess[str]:
    argv = ["sh", str(CODEGEN / "check_fresh.sh"), sys.executable, str(copy)]
    return subprocess.run(argv, capture_output=True, text=True, check=False, cwd=ROOT)


def _copy(tmp_path: Path) -> Path:
    copy = tmp_path / "generated"
    shutil.copytree(GENERATED, copy, ignore=shutil.ignore_patterns("__pycache__"))
    return copy


def _symlink_extra(copy: Path) -> None:
    (copy / "extra.py").symlink_to(GENERATED.parent / "hermes_merge.py")


def _dangling(copy: Path) -> None:
    (copy / "dangling.py").symlink_to(copy / "no_such_file.py")


def _empty_dir(copy: Path) -> None:
    (copy / "sub").mkdir()


def _dir_symlink(copy: Path) -> None:
    (copy / "up").symlink_to(copy.parent, target_is_directory=True)


@pytest.mark.parametrize(
    "damage", [_symlink_extra, _dangling, _empty_dir, _dir_symlink], ids=["symlink", "dangling", "empty", "dir-link"]
)
def test_extra_entries_fail(tmp_path: Path, damage: Callable[[Path], None]) -> None:
    copy = _copy(tmp_path)
    assert _run(copy).returncode == 0
    damage(copy)
    done = _run(copy)
    assert done.returncode == 1 and "Only in" in done.stdout, done.stdout + done.stderr


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="known gap: -x __pycache__ skips any file below it")
def test_hand_written_module_below_pycache_fails(tmp_path: Path) -> None:
    # glasses.generated.__pycache__.helpers imports as a namespace package; ruff excludes it (**/generated/**).
    copy = _copy(tmp_path)
    (copy / "__pycache__").mkdir()
    (copy / "__pycache__/helpers.py").write_text(HAND_WRITTEN, encoding="utf-8")
    assert _run(copy).returncode == 1


def test_bytecode_cache_is_ignored(tmp_path: Path) -> None:
    copy = _copy(tmp_path)
    (copy / "__pycache__").mkdir()
    (copy / "__pycache__/protocol_types.cpython-314.pyc").write_bytes(b"\x00")
    assert _run(copy).returncode == 0
