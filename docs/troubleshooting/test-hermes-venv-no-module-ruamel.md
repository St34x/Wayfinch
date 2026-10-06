<!-- SPDX-License-Identifier: Apache-2.0 -->
# Test Hermes venv: `No module named 'ruamel'`

## Symptom
`pip install -e "./hermes-agent[...]"` exits 0, but every `hermes` command (or the test gateway) fails with
`ModuleNotFoundError: No module named 'ruamel'`; `pip list` in the venv shows only a handful of packages.

## Context
Building the venv of the pinned Hermes Agent clone for the integration test instance
([`../test-hermes.md`](../test-hermes.md)) with a Python older than 3.14.

## Cause
Every core dependency of the pinned Hermes carries the marker `python_version >= '3.14'`, so pip skips all of them on
an older interpreter. The `requires-python` range (`>=3.11,<3.15`) still lets the install itself succeed.

## Fix
Create the venv with Python 3.14 (`python3.14 -m venv .local/venv314`) and install the clone again.

## Verified
Verified with Hermes `a4648c5f58`, 2026-10-04: the install on Python 3.12 left the gateway failing with the error
above; on Python 3.14 the same install works (re-checked 2026-10-06: `hermes --version` prints `Python: 3.14.7`).

## Related
`test-hermes-venv-pyyaml-missing.md`
