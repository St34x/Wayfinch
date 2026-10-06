# SPDX-License-Identifier: Apache-2.0
"""tools/codegen: the CLI on a scratch root, as check-codegen calls it.

Every case copies protocol/envelope.schema.json and protocol/README.md into a scratch root, edits the copy and runs
``gen_protocol.py --root <scratch> --out-dir <dir>`` with this venv's interpreter.
"""

from __future__ import annotations

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
COMMITTED = ROOT / "hermes-plugin/glasses/generated/protocol_types.py"
Mutation = Callable[[Any], None]


def _run(tmp_path: Path, mutate: Mutation | None = None) -> tuple[subprocess.CompletedProcess[str], Path]:
    (tmp_path / "protocol").mkdir()
    shutil.copy(ROOT / "protocol/README.md", tmp_path / "protocol/README.md")
    schema: Any = json.loads((ROOT / "protocol/envelope.schema.json").read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(schema)
    (tmp_path / "protocol/envelope.schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
    out = tmp_path / "out"
    argv = [sys.executable, str(CODEGEN / "gen_protocol.py"), "--root", str(tmp_path), "--out-dir", str(out)]
    return subprocess.run(argv, capture_output=True, text=True, check=False), out / "protocol_types.py"


def _prop(body: str, prop: str, key: str, value: object) -> Mutation:
    return lambda s: s["$defs"][f"body.{body}"]["properties"][prop].__setitem__(key, value)


def test_unchanged_copy_reproduces_the_committed_file(tmp_path: Path) -> None:
    done, out = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") == COMMITTED.read_text(encoding="utf-8")


def test_new_property_changes_the_output(tmp_path: Path) -> None:
    done, out = _run(tmp_path, lambda s: s["$defs"]["body.ping"]["properties"].__setitem__("probe", {"type": "string"}))
    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") != COMMITTED.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_prop("ack", "upToSeq", "format", "int64"), "#/$defs/body.ack/properties/upToSeq/format: unsupported"),
        (_prop("ack", "upToSeq", "multipleOf", 2), "#/$defs/body.ack/properties/upToSeq/multipleOf: unsupported"),
        (_prop("ack", "upToSeq", "type", ["integer", "null"]), "#/$defs/body.ack/properties/upToSeq/type"),
        (lambda s: s["$defs"]["body.ping"].__setitem__("$defs", {}), "#/$defs/body.ping/$defs: unsupported"),
        (
            lambda s: s["$defs"]["body.ack"].update({"if": {"required": ["nope"]}, "then": {}}),
            "#/$defs/body.ack/if/required: 'nope' is not a declared property",
        ),
    ],
)
def test_cli_rejects_with_the_path(tmp_path: Path, mutate: Mutation, expected: str) -> None:
    done, out = _run(tmp_path, mutate)
    assert done.returncode == 2, done.stdout + done.stderr
    assert expected in done.stderr
    assert not out.exists()


def test_missing_schema_fails(tmp_path: Path) -> None:
    argv = [sys.executable, str(CODEGEN / "gen_protocol.py"), "--root", str(tmp_path), "--out-dir", str(tmp_path / "o")]
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert done.returncode == 2
    missing = tmp_path / "protocol/envelope.schema.json"
    assert done.stderr == f"gen_protocol: {missing}: cannot read: No such file or directory\n"
    assert not (tmp_path / "o").exists()
