<!-- SPDX-License-Identifier: Apache-2.0 -->
# import-linter: a forbidden contract lets unlisted I/O into the core

## Symptom
A core module with `import io` / `io.open(...)`, `import codecs`, `sys.stdout.write(...)`, `datetime.now()` or
`import requests` passes: `Contracts: 1 kept, 0 broken.`

## Context
The `core-purity` contract in `.importlinter` is of type `forbidden`: it lists what the core must not import.

## Cause
A forbidden contract is a denylist. Every module nobody thought of (`io`, `codecs`, `sys`, any third-party I/O
library, any module added later) passes, and calls without an import edge (`datetime.now()` on an allowed module,
`sys.stdout` attributes) are invisible to an import graph.

## Fix
Check the direct imports of every core module against an ALLOWLIST (fail closed) with an AST check:
`tools/quality/check_core_purity.py` allows only core modules, `glasses.generated`, the modules in
`STDLIB_ALLOWLIST` (one line with a reason each) and ratchet entries of the contract; it also bans builtin I/O
names (also `globals`/`vars`/`locals` and constant `getattr` names), clock reads (`.now`/`.utcnow`/`.today`,
`time.*` unless `from datetime import time`) and console streams (`.stdout` ...). A submodule reached as an
attribute is allowlisted like an import: `import urllib.parse` binds `urllib`, so `urllib.request.urlopen(...)`
would otherwise pass both checks. Keep the forbidden contract for the transitive part (I/O reached through a
generated or ratcheted module). To allow a new pure stdlib module, add it to `STDLIB_ALLOWLIST` with its reason.

## Verified
Verified with import-linter 2.15: each import above planted in the real core module fails `check_core_purity.py`
(rc 1), the unplanted tree passes; `tools/quality/tests/test_imports*.py`. Also: `import urllib.parse` plus
`urllib.request.urlopen(...)` in the core module → rc 1 (lint-imports still kept).

## Related
`import-linter-contract-errors.md`; ENGINEERING.md section 1; `.importlinter` header.
