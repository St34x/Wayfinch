#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate the Kotlin protocol types from protocol/envelope.schema.json (ENGINEERING.md section 4). Stdlib only.

Output: android/protocol/src/main/kotlin/app/hudcompanion/protocol/generated/Envelope.kt (`@generated` header, SPDX;
the text comes from kotlin_emitter.py, the model from the same reader as the Python types, gen_protocol.load_model).
The generator owns that directory's only file: `--check` (run by `make check-codegen`) compares it with a fresh render
and fails when it is missing, edited, stale or a symlink, or when the directory holds any other entry (no hand-written
envelope models beside the generated ones).
The generated decoder is strict, not just typed: duplicate keys, nulls, quoted or fractional integers, integers outside
the schema's bounds and quoted booleans are refused (design and reasons: kotlin_runtime.py docstring).

Usage: gen_kotlin.py [--root DIR] [--out-dir DIR] [--check]   (defaults: this repository, its generated directory)
Exit:  0 written / fresh; 1 `--check` and the file is not fresh (unified diff on stdout); 2 an input cannot be read, or
       the schema uses something the generator cannot represent (one line on stderr naming the file / JSON pointer).
"""

import argparse
import difflib
import sys
from pathlib import Path

from gen_protocol import SCHEMA, load_model
from kotlin_emitter import emit_kotlin
from protocol_model import CodegenError

OUTPUT_DIR = Path("android/protocol/src/main/kotlin/app/hudcompanion/protocol/generated")
OUTPUT = OUTPUT_DIR / "Envelope.kt"
REGENERATE = "python3 tools/codegen/gen_kotlin.py"


def generate(root: Path) -> str:
    """The text of Envelope.kt for the schema and README under root (raises CodegenError)."""
    return emit_kotlin(load_model(root))


def _write(out: Path, text: str) -> None:
    """Write the generated file; a symlink in its place is replaced, never written through."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.is_symlink():
            out.unlink()
        out.write_text(text, encoding="utf-8")
    except OSError as error:
        raise CodegenError(f"{error.filename or out}: cannot write: {error.strerror or error}") from None


def _not_fresh(out: Path, text: str) -> str:
    """Why out is not the generated file ("" = fresh); prints a unified diff when its content differs."""
    if out.is_symlink():
        return "is a symlink, not the generated file"
    if not out.exists():
        return "is missing"
    if not out.is_file():
        return "is not a regular file"
    try:
        current = out.read_bytes()
    except OSError as error:
        raise CodegenError(f"{out}: cannot read: {error.strerror or error}") from None
    if current == text.encode("utf-8"):
        return ""
    old = current.decode("utf-8", errors="replace").splitlines(keepends=True)
    sys.stdout.writelines(difflib.unified_diff(old, text.splitlines(keepends=True), str(out), f"{out} (generated)"))
    return "is stale (edited, or the schema changed)"


def check(out: Path, text: str) -> int:
    """Compare out with the generated text: 0 fresh, 1 not (one line on stderr naming the fix)."""
    problem = _not_fresh(out, text)
    if problem:
        sys.stderr.write(f"check-codegen: {out} {problem}; run: {REGENERATE}\n")
        return 1
    extra = sorted(entry.name for entry in out.parent.iterdir() if entry.name != out.name)
    if extra:
        names = ", ".join(extra)
        sys.stderr.write(f"check-codegen: {out.parent} holds {names}, not written by the generator; delete it\n")
        return 1
    sys.stdout.write(f"check-codegen: {out} matches the generator\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the Kotlin protocol types from the envelope schema.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--out-dir", type=Path, help=f"directory of {OUTPUT.name} (default: <root>/{OUTPUT_DIR})")
    parser.add_argument("--check", action="store_true", help="compare instead of writing (exit 1 when stale)")
    args = parser.parse_args(argv)
    out = (args.out_dir or args.root / OUTPUT_DIR) / OUTPUT.name
    try:
        text = generate(args.root)
        if args.check:
            return check(out, text)
        _write(out, text)
    except CodegenError as error:
        where = SCHEMA.as_posix() if str(error).startswith("#") else ""
        sys.stderr.write(f"gen_kotlin: {where}{error}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
