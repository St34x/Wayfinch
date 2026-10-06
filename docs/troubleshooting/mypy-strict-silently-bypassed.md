<!-- SPDX-License-Identifier: Apache-2.0 -->
# mypy `--strict` passes although a file has untyped code

## Symptom
`make typecheck-py` (or `mypy --strict`) prints `Success: no issues found` while a checked file holds an untyped
function. With this repository's mypy-config check, the gate fails instead with
`check-mypy-config: <path>:<line>: ...` or `check-mypy-config: [<section>] must hold exactly {...}`.

## Context
mypy reads its settings from three places: the config file, inline `# mypy:` comments in each source file, and
`# type: ignore` comments. `strict = True` in `[mypy]` is only the default for every module.

## Cause
Each of these silently turns checking off for part of the tree (verified with mypy 2.4.0):
- a per-module section with `disable_error_code = ...`, `ignore_errors`, `follow_imports = skip`, or `strict = False`;
- a first line `# mypy: ignore-errors` (inline configuration) in the file;
- a bare `# type: ignore` on a line of its own before the first statement: the whole module is ignored, and
  `enable_error_code = ignore-without-code` does **not** report it (it only reports bare ignores on code lines);
- the `typing.no_type_check` decorator on a function;
- a stub `foo.pyi` next to `foo.py`: mypy checks the stub and never reads the `.py`;
- a file mypy's directory crawl skips: any path part starting with `.`, `__pycache__`, `site-packages`,
  `node_modules`, or a `foo.py` next to a directory `foo/`;
- code behind a name mypy's reachability analysis treats as a constant: `TYPE_CHECKING`/`MYPY` (true for mypy, false
  at runtime), `PY2`/`PY3`. The body of `if not TYPE_CHECKING:`, the `else`/`elif` after `if TYPE_CHECKING:`, and
  everything after `if TYPE_CHECKING: raise ...` run but are never checked.

## Fix
`tools/quality/check_mypy_config.py` (run first by `make typecheck-py`) allows only the expected `mypy.ini` sections
with exactly their keys (fixed `[mypy]`, the Hermes boundary, the adapter section, ratchet blocks of
`ignore_errors = True` whose ids/modules are a subset of its `RATCHET`), and scans every checked `*.py`/`*.pyi` for
inline mypy configuration, a type-ignore comment on its own line and the `no_type_check` decorator. It also fails on
any `*.pyi`, on files the crawl skips, and on every use of those constant names except an imports-only
`if TYPE_CHECKING:` without else. A checked file that is not UTF-8 ends the gate with exit 2. `mypy.ini` also
enables `warn_unused_ignores` and `ignore-without-code`, so an ignore on a code line must name its error code.
A justified suppression is `# type: ignore[<code>]` on the offending line, with the reason in a comment.

## Verified
Verified with mypy 2.4.0, 2026-10-05: each bypass planted in the worktree makes `make typecheck-py` fail; the tests
are in `tools/quality/tests/test_mypy_config.py` and `test_mypy_untyped_bypass.py`.

## Related
`mypy-tools-namespace-clash.md`, `mypy.ini`, `mk/typecheck-py.mk`.
