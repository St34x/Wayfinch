<!-- SPDX-License-Identifier: Apache-2.0 -->
# ruff prints rule names instead of rule codes

## Symptom
`make lint-py` (or `ruff check`) reports `too-many-arguments: Too many arguments in function definition (5 > 4)` or
`complex-structure: ... is too complex (11 > 10)` with no `PLR0913` / `C901`, so the code to look up or to name in a
`noqa` comment is missing.

## Context
`ruff.toml` sets `preview = true` (needed for the preview rule `PLR1702`, opted in by `explicit-preview-rules`).
ruff 0.16 in preview mode shows the human-readable rule name in every output format except JSON.

## Cause
Preview behaviour of ruff, not a configuration error.

## Fix
`output-prefer-rule-codes = true` at the top level of `ruff.toml` (already set in this repository). A one-off run:
`ruff check --config 'output-prefer-rule-codes = true' ...`.

## Verified
ruff 0.16.10: a planted 5-parameter function printed `too-many-arguments: ...` without the setting and
`PLR0913 Too many arguments in function definition (5 > 4)` with it (`--output-format concise` and default).

## Related
`ruff.toml`, `mk/lint-py.mk`, `ENGINEERING.md` section 2.
