# SPDX-License-Identifier: Apache-2.0
"""Freshness edge cases of tools/codegen/gen_config_docs.py beyond test_gen_config_docs.py (hidden file and
empty subdirectory next to the doc, which the generator does not own; symlinked doc) and the schema `examples`
path into the doc.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import gen_config_docs as gen
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools/codegen/gen_config_docs.py"


def _check(out_dir: Path) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT), "--check", "--out-dir", str(out_dir)]
    return subprocess.run(argv, capture_output=True, text=True, check=False, cwd=ROOT)


def _fresh_copy(tmp_path: Path) -> Path:
    out = tmp_path / "reference"
    shutil.copytree(ROOT / gen.OUTPUT_DIR, out)
    assert _check(out).returncode == 0
    return out


def test_hidden_file_next_to_the_doc_is_not_owned(tmp_path: Path) -> None:
    out = _fresh_copy(tmp_path)
    (out / ".notes").write_text("x\n", encoding="utf-8")
    assert _check(out).returncode == 0


def test_empty_subdirectory_next_to_the_doc_is_not_owned(tmp_path: Path) -> None:
    out = _fresh_copy(tmp_path)
    (out / "sub").mkdir()
    assert _check(out).returncode == 0


def test_stale_message_is_one_line_naming_the_command(tmp_path: Path) -> None:
    out = _fresh_copy(tmp_path)
    with (out / gen.OUTPUT.name).open("a", encoding="utf-8") as handle:
        handle.write("edited\n")
    done = _check(out)
    assert done.returncode == 1
    assert done.stderr.count("\n") == 1
    assert gen.REGENERATE in done.stderr


def test_symlinked_doc_fails(tmp_path: Path) -> None:
    out = _fresh_copy(tmp_path)
    target = tmp_path / "elsewhere.md"
    shutil.move(out / gen.OUTPUT.name, target)
    (out / gen.OUTPUT.name).symlink_to(target)
    assert _check(out).returncode == 1


def test_example_yaml_round_trips_every_example_value() -> None:
    text = (ROOT / gen.OUTPUT).read_text(encoding="utf-8")
    block = text.split("```yaml\n", 1)[1].split("```", 1)[0]
    extra = yaml.safe_load(block)["gateway"]["platforms"]["glasses"]["extra"]
    schema = json.loads((ROOT / gen.SCHEMA).read_text(encoding="utf-8"))
    for key in gen.walk(schema["properties"], schema.get("$defs", {})):
        if not gen.has_example(key):
            continue
        value: object = extra
        for part in key.path.split("."):
            assert isinstance(value, dict), key.path
            value = value[part]
        assert value == gen.example(key), key.path
