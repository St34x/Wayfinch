<!-- SPDX-License-Identifier: Apache-2.0 -->
# import-linter: contract errors that are not boundary violations

## Symptom
`lint-imports` exits 1 with one of these instead of a list of broken contracts:
- `Module 'glasses.envelope' does not exist.`
- `No matches for ignored import glasses.hermes_merge -> glasses.config.`
- `Invalid forbidden module urllib.request: subpackages of external packages are not valid.`
- `forbidden_modules: A wildcard can only replace a whole module.`
- `Could not find package 'glasses' in your Python path.` (run from the repository top instead of `hermes-plugin/`).

## Context
`make check-imports` runs import-linter 2.15 (grimp 3.17) on the contract `core-purity` in `.importlinter`
(`include_external_packages = True`), from `hermes-plugin/`, where the root package `glasses` lives.

## Cause
- A module listed in `source_modules` must exist; a listed `forbidden_modules` entry that does not exist yet is
  accepted silently (that is how future adapters such as `glasses.server` are pre-listed).
- An `ignore_imports` entry that matches no import is an error by default (stale ratchet entry).
- External packages (stdlib included) are graph nodes only at the top level, so only `urllib`, not
  `urllib.request`, can be forbidden. `tools/quality/check_core_purity.py` checks direct imports of core modules
  against an allowlist instead (only `urllib.parse` of `urllib` is on it).
- Wildcards replace whole name components only (`glasses.telegram*` is invalid; `glasses.telegram` covers the
  package and its submodules).
- lint-imports puts only the current directory on `sys.path`.

## Fix
Add a core module to `source_modules` in the same change that creates it; delete an ignore entry when its import
is gone; forbid external packages by their top-level name; run from `hermes-plugin/` (`make check-imports`). Never
put a blank line inside a list value: the reader splits the value on newlines.

## Verified
Verified with import-linter 2.15: each message reproduced on a scratch package and on a copy of the plugin
(`tools/quality/tests/test_imports.py`). Also verified there: stdlib imports are seen (direct, through another
module and inside functions); imports inside stdlib modules are not followed (`import logging` does not count
as `os`); `__import__("socket")` and `open()` are invisible to the graph (the AST part catches them).

## Related
ENGINEERING.md section 1; `.importlinter` header; `mk/imports.mk`; `import-linter-denylist-passes-io.md`.
