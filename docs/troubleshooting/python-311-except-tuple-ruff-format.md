<!-- SPDX-License-Identifier: Apache-2.0 -->
# Plugin does not load on Python 3.11: "multiple exception types must be parenthesized"

## Symptom
The Hermes gateway on Python 3.11 (or 3.12/3.13) fails to import the glasses plugin with
`SyntaxError: multiple exception types must be parenthesized`, pointing at a line `except A, B:`. Every gate and test
is green, because the dev venv and the test Hermes run Python 3.14.

## Context
The plugin runs inside the Hermes venv; Hermes supports Python 3.11 and newer (`requires-python >=3.11`). Code is
formatted by `ruff format` with the `target-version` of `ruff.toml`.

## Cause
With `target-version = "py314"`, `ruff format` removes the parentheses of `except (A, B):` and writes the PEP 758 form
`except A, B:`, valid only from Python 3.14. Nothing older than the dev venv compiled the code.

## Fix
Keep `target-version = "py311"` in `ruff.toml` and `python_version = 3.11` in `mypy.ini` (ENGINEERING.md section 8).
Then `make lint-py` reports newer syntax as `invalid-syntax: ... on Python 3.11 (syntax was added in Python 3.14)`,
`ruff format` keeps the parentheses, and `make typecheck-py` rejects stdlib APIs added after 3.11
(`Module has no attribute "batched"`), except in modules under a mypy ratchet block in `mypy.ini`
(`ignore_errors = True`; `mypy.ini` lists the current blocks) until the owning task removes its block;
`tools/quality/tests/test_ruff_python_floor.py` (run by `make lint-py`) syntax-checks every tracked `*.py` for 3.11,
generated code and ratchet-block modules included. Restore the parentheses by hand: `ruff check
--fix` does not rewrite them.
Quick check of the plugin with any 3.11 interpreter: `python3.11 -c 'import pathlib,sys; [compile(p.read_bytes(),
str(p), "exec") for p in pathlib.Path("hermes-plugin").rglob("*.py")]'` (no output = every file compiles).

## Verified
ruff 0.16.10, mypy 2.4.0, CPython 3.11.15: before the fix four plugin modules and one tool failed `compile()` on 3.11;
after it every tracked `*.py` compiles. With the floor reverted to py314/3.14 the planted-code tests in
`tools/quality/tests/test_*_python_floor.py` fail; with one `except A, B:` restored in a tool, the tracked-files check
fails.

## Related
`ruff.toml`, `mypy.ini`, `tools/quality/check_mypy_config.py`, `ENGINEERING.md` sections 8 and 10.
