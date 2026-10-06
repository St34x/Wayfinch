<!-- SPDX-License-Identifier: Apache-2.0 -->
# mypy: `Module "tools" has no attribute ...` for a Hermes import

## Symptom
`error: Module "tools" has no attribute "slash_confirm"  [attr-defined]` from `make typecheck-py` (mypy), on a line
such as `from tools import slash_confirm`, although `mypy.ini` has `[mypy-tools.*] ignore_missing_imports = True`.

## Context
Hermes ships a top-level `tools` package; this repository has a `tools/` directory (not a package) at the root, and
mypy runs from the root.

## Cause
With `namespace_packages` on (the mypy default), mypy resolves `tools` to our directory as an implicit namespace
package, so the import is "found" and `ignore_missing_imports` never applies. `from tools.x import y` is silenced;
`from tools import x` is checked against our directory and fails.

## Fix
`namespace_packages = False` in the `[mypy]` section (set in `mypy.ini`). Our files are still checked: mypy gets them
through `files = ...` and names each one by crawling up through `__init__.py` files (`tools/quality/check_size.py` is
module `check_size`).

## Verified
mypy 2.4.0, 2026-10-05: the error disappears with the option; `test_hermes_tools_import_is_not_our_tools_dir` and the
planted-error tests in `tools/quality/tests/test_mypy_baseline.py` prove the import passes and `tools/` files still
fail on a planted untyped function.

## Related
`mypy.ini`, `mk/typecheck-py.mk`.
