<!-- SPDX-License-Identifier: Apache-2.0 -->
# Glasses platform refused: jsonschema not installed (or too old)

## Symptom
The glasses platform does not start; the gateway log has one line
`glasses config invalid: the Python module 'jsonschema' is not installed in this Python — the glasses plugin needs
it; install it into the Hermes venv (pip install jsonschema)`. `python -m glasses.admin config check` prints the
same text after `error: ` and exits with status 2. The same line also appears when `jsonschema` *is* installed but
older than 4.0; then `pip install jsonschema` only reports "Requirement already satisfied" and the error stays.

## Context
Hermes installs without its optional `mcp` extra do not ship `jsonschema`; the glasses plugin validates its
settings with `config.schema.json` (JSON Schema draft 2020-12) and needs it. Some environments carry an old
`jsonschema` 3.x pulled in by another package.

## Cause
The plugin's settings validation imports `jsonschema` lazily and needs its `Draft202012Validator`. Two cases give
the same line, and in both the plugin refuses the platform (fail closed) instead of starting with unvalidated
settings:
- `jsonschema` is missing from the Python that runs the gateway;
- `jsonschema` is older than 4.0 and has no `Draft202012Validator` (draft 2020-12 support came with 4.0).

## Fix
Install or upgrade `jsonschema` into the Python environment that runs the Hermes gateway (the Hermes venv), e.g.
`<hermes-venv>/bin/python -m pip install -U 'jsonschema>=4'` (the project tests against the version pinned in
`docs/dependencies.md`), then restart the gateway. Check the installed version with
`<hermes-venv>/bin/python -m pip show jsonschema`. Run `config check` with that same Python.

## Verified
Plugin tests with `jsonschema` blocked (tests/test_config_missing_deps.py): one ERROR log line, `config check`
exit 2 with one line and no traceback. Plugin tests with a `jsonschema` stand-in that lacks `Draft202012Validator`
(tests/test_config_values_core.py): the same `DependencyMissing("jsonschema")` result.

## Related
`hermes-gateway-exit-78-plugin-startup.md`
