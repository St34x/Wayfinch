<!-- SPDX-License-Identifier: Apache-2.0 -->
# `config check`: `the Python module 'PyYAML' is not installed in this Python`

## Symptom
`python -m glasses.admin config check --live` (exit 2), or the live tests in
`hermes-plugin/tests/test_config_check_live.py`, report:
`error: the Python module 'PyYAML' is not installed in this Python — the glasses plugin needs it; install it into the
Hermes venv (pip install PyYAML)`.

## Context
The operator CLI runs with the Python of the Hermes install it checks. Hermes itself reads YAML with `ruamel.yaml`;
`PyYAML` reaches a Hermes venv only through extras that happen to depend on it (for example the local
speech-to-text extras), not through `messaging` or `mcp`.

## Cause
The plugin's operator CLI reads `config.yaml` with `PyYAML`, and the Hermes venv was built without an extra that
pulls it in.

## Fix
Install it into that venv with the version the dev venv pins: `<hermes venv>/bin/python -m pip install PyYAML==6.0.3`.
The integration test recipe ([`../test-hermes.md`](../test-hermes.md)) includes this step.

## Verified
Verified on a scratch venv of Hermes `a4648c5f58` with `[messaging,mcp]`, 2026-10-06: without PyYAML two live tests
failed with the message above; after the install all 26 tests of `test_config_check_live.py` passed.

## Related
`test-hermes-venv-no-module-ruamel.md`, `glasses-jsonschema-not-installed.md`
