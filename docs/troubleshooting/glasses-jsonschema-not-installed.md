<!-- SPDX-License-Identifier: Apache-2.0 -->
# Glasses platform refused: jsonschema not installed

## Symptom
The glasses platform does not start; the gateway log has one line
`glasses config invalid: the Python module 'jsonschema' is not installed in this Python — the glasses plugin needs
it; install it into the Hermes venv (pip install jsonschema)`. `python -m glasses.admin config check` prints the
same text after `error: ` and exits with status 2.

## Context
Hermes installs without its optional `mcp` extra do not ship `jsonschema`; the glasses plugin validates its
settings with `config.schema.json` and needs it.

## Cause
The plugin's settings validation imports `jsonschema` lazily; when it is missing the plugin refuses the platform
(fail closed) instead of starting with unvalidated settings.

## Fix
Install `jsonschema` into the Python environment that runs the Hermes gateway (the Hermes venv), e.g.
`<hermes-venv>/bin/python -m pip install jsonschema`, then restart the gateway. Run `config check` with that same
Python.

## Verified
Plugin tests with `jsonschema` blocked (tests/test_config_missing_deps.py): one ERROR log line, `config check`
exit 2 with one line and no traceback.

## Related
`hermes-gateway-exit-78-plugin-startup.md`
