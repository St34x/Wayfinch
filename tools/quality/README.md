<!-- SPDX-License-Identifier: Apache-2.0 -->
# Quality gates

## Purpose

Checkers behind `make test` that enforce `ENGINEERING.md`: size limits, repository hygiene (task ids, glossary, no
private references), module READMEs, mypy configuration, core purity and plugin coverage. Each one is a stdlib
script with its `mk/*.mk` fragment and tests.

## Public API

| Script | Make target | Data files |
|---|---|---|
| `check_size.py` (sh function rules: `size_sh.py`) | `check-size` | `size-exempt.txt`, `size-baseline.txt` |
| `check_hygiene.py` (`--commits`: at merge time) | `check-hygiene` | `commit-gate-start.txt` |
| `check_readmes.py` | `check-hygiene` | `modules.txt` |
| `check_mypy_config.py` | `typecheck-py` | `mypy.ini` (root) |
| `check_core_purity.py` | `check-imports` | `.importlinter` (root) |
| `coverage_areas.py` | `coverage-py` | `.coveragerc` (root) |

ruff (`lint-py`) is configured in the root `ruff.toml`; its baseline and bypass tests live in `tests/`.
Every script prints `<gate>: OK`, most with a summary in parentheses, or one finding per line on stderr.

## Invariants

- Exit 0 = clean, 1 = findings, 2 = cannot check (e.g. a missing config or data file, a git error).
- Allowlists, not denylists; exemptions are structural (exact paths, leading `@generated` header) and every
  ratchet baseline entry names the task id that removes it; a stale entry fails.
- Every test file of a gate is picked up by a glob in its fragment, and an empty glob fails the target.
- Each gate has a test that runs it over the real tree.
- Checkers never import the code they scan.
