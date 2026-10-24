# SPDX-License-Identifier: Apache-2.0
"""tools/codegen: gen_kotlin.py on a scratch root, as `make check-codegen` calls it (T1.105).

Every case copies protocol/envelope.schema.json and protocol/README.md into a scratch root, edits the copy and runs
``gen_kotlin.py --root <scratch> --out-dir <dir> [--check]`` with this venv's interpreter.
"""

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

CODEGEN = Path(__file__).resolve().parents[1]
ROOT = CODEGEN.parents[1]
COMMITTED = ROOT / "android/protocol/src/main/kotlin/app/hudcompanion/protocol/generated/Envelope.kt"
Mutation = Callable[[Any], None]


def _scratch(tmp_path: Path, mutate: Mutation | None = None) -> Path:
    (tmp_path / "protocol").mkdir()
    shutil.copy(ROOT / "protocol/README.md", tmp_path / "protocol/README.md")
    schema: Any = json.loads((ROOT / "protocol/envelope.schema.json").read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(schema)
    (tmp_path / "protocol/envelope.schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
    return tmp_path


def _run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(CODEGEN / "gen_kotlin.py"), *map(str, args)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _generate(root: Path, out_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run("--root", root, "--out-dir", out_dir)


def _fresh_dir(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    shutil.copy(COMMITTED, out / "Envelope.kt")
    return out


def test_unchanged_copy_reproduces_the_committed_file(tmp_path: Path) -> None:
    done = _generate(_scratch(tmp_path), tmp_path / "out")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "out/Envelope.kt").read_bytes() == COMMITTED.read_bytes()


def test_the_real_tree_is_fresh() -> None:
    done = _run("--check")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "matches the generator" in done.stdout


def test_check_passes_on_a_fresh_file(tmp_path: Path) -> None:
    done = _run("--check", "--root", ROOT, "--out-dir", _fresh_dir(tmp_path))
    assert done.returncode == 0, done.stderr


def test_a_schema_edit_without_regeneration_fails_the_check(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = _scratch(scratch, lambda s: s["$defs"]["body.ack"]["properties"].__setitem__("probe", {"type": "string"}))
    done = _run("--check", "--root", root, "--out-dir", _fresh_dir(tmp_path))
    assert done.returncode == 1
    assert "is stale" in done.stderr and "python3 tools/codegen/gen_kotlin.py" in done.stderr
    assert "+    public val probe: String? = null," in done.stdout


def test_a_hand_edit_fails_the_check(tmp_path: Path) -> None:
    out = _fresh_dir(tmp_path)
    (out / "Envelope.kt").write_text(COMMITTED.read_text(encoding="utf-8") + "// local tweak\n", encoding="utf-8")
    done = _run("--check", "--root", ROOT, "--out-dir", out)
    assert done.returncode == 1
    assert "-// local tweak" in done.stdout


def test_a_missing_file_fails_the_check(tmp_path: Path) -> None:
    done = _run("--check", "--root", ROOT, "--out-dir", tmp_path)
    assert done.returncode == 1
    assert "is missing" in done.stderr


def test_a_symlink_in_place_of_the_file_fails_the_check_and_is_replaced_on_write(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.kt"
    shutil.copy(COMMITTED, target)
    out = tmp_path / "out"
    out.mkdir()
    (out / "Envelope.kt").symlink_to(target)
    assert "is a symlink" in _run("--check", "--root", ROOT, "--out-dir", out).stderr
    assert _generate(ROOT, out).returncode == 0
    assert not (out / "Envelope.kt").is_symlink()
    assert target.read_bytes() == COMMITTED.read_bytes()


def test_a_directory_in_place_of_the_file_fails_the_check(tmp_path: Path) -> None:
    (tmp_path / "Envelope.kt").mkdir()
    done = _run("--check", "--root", ROOT, "--out-dir", tmp_path)
    assert done.returncode == 1
    assert "is not a regular file" in done.stderr


def test_a_hand_written_file_beside_the_generated_one_fails_the_check(tmp_path: Path) -> None:
    out = _fresh_dir(tmp_path)
    (out / "Extra.kt").write_text("// SPDX-License-Identifier: Apache-2.0\nclass Extra\n", encoding="utf-8")
    done = _run("--check", "--root", ROOT, "--out-dir", out)
    assert done.returncode == 1
    assert "Extra.kt" in done.stderr and "not written by the generator" in done.stderr


def _stub_python(tmp_path: Path, exit_code: int) -> tuple[Path, Path]:
    log = tmp_path / "calls.log"
    stub = tmp_path / "python-stub"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{log}"\nexit {exit_code}\n', encoding="utf-8")
    stub.chmod(0o755)
    return stub, log


def _regenerate(stub: Path) -> subprocess.CompletedProcess[str]:
    argv = ["sh", str(CODEGEN / "regenerate.sh"), str(stub)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_regenerate_script_runs_both_generators_in_order(tmp_path: Path) -> None:
    stub, log = _stub_python(tmp_path, 0)
    done = _regenerate(stub)
    assert done.returncode == 0, done.stderr
    assert [Path(line).name for line in log.read_text(encoding="utf-8").split()] == ["gen_protocol.py", "gen_kotlin.py"]


def test_regenerate_script_stops_at_the_first_failing_generator(tmp_path: Path) -> None:
    stub, log = _stub_python(tmp_path, 3)
    done = _regenerate(stub)
    assert done.returncode == 3
    assert [Path(line).name for line in log.read_text(encoding="utf-8").split()] == ["gen_protocol.py"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s["$defs"]["body.ack"]["properties"]["upToSeq"].__setitem__("format", "int64"), "upToSeq/format"),
        (lambda s: s["$defs"]["body.ack"]["properties"]["upToSeq"].__setitem__("minimum", 1.5), "upToSeq/minimum"),
        (lambda s: s["$defs"]["body.ping"]["properties"].__setitem__("class", {"type": "string"}), "'class'"),
    ],
)
def test_schema_problems_exit_2_naming_the_pointer(tmp_path: Path, mutate: Mutation, expected: str) -> None:
    done = _generate(_scratch(tmp_path, mutate), tmp_path / "out")
    assert done.returncode == 2
    assert expected in done.stderr and done.stderr.startswith("gen_kotlin: ")
    assert not (tmp_path / "out").exists()


def test_a_missing_input_exits_2(tmp_path: Path) -> None:
    done = _generate(tmp_path, tmp_path / "out")
    assert done.returncode == 2
    assert "cannot read" in done.stderr
