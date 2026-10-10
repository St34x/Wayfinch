<!-- SPDX-License-Identifier: Apache-2.0 -->
# glasses — Hermes gateway platform plugin

## Purpose

The `glasses` platform of the Hermes gateway and its operator CLI. Today it holds config validation, the device
registry, the allow-all gate, the envelope codec (not called yet) and a fail-closed adapter skeleton: `connect()`
reports a retryable error and `send()` delivers nothing. Planned, not built: the protocol server (`protocol/README.md`).

## Public API

- `register(ctx)` (`__init__.py`) — registers the platform; manifest in `plugin.yaml`.
- `adapter.py` — the adapter the gateway builds (subclasses Hermes' `BasePlatformAdapter`); skeleton, see Purpose.
- `config*.py` + `config.schema.json` — settings under `gateway.platforms.glasses.extra`: `config.load()`, `check()`.
- `gate.py` — allow-all / wildcard gate (`violations()`), read the way the gateway reads it.
- `registry.py` — device registry `devices.json` (`add`, `verify`, `revoke`).
- Envelope codec, core; schema injected (`FrameSchema`; loading it is planned in T1.07): `envelope.py` `decode`,
  `error_frame`; `envelope_scan.py` caps, nesting pre-scan; `envelope_rules.py` `violation`; `envelope_encode.py`
  `encode` (`EncodeRefused`: schema, rules, cap, device-only type); `envelope_text.py` `cut_utf8`, `sanitize_card_text`.
- Operator CLI, only command `config check` (`admin.py`, `hermes_merge.py`, `hermes_facts.py`): `cd hermes-plugin &&
  "$(dirname "$PWD")/.local/venv-dev/bin/python" -m glasses.admin config check [--config FILE] [--hermes-home DIR]`
  (absolute path: a `..` makes Python 3.14 warn); `--live` is **not read-only**, details and known gap in `admin.py`.
- `generated/` — protocol `TypedDict`s, written by `tools/codegen/` only.

## Invariants

- Importing the package or calling `register` imports no Hermes module. Hermes is imported by `adapter.py`, by
  `hermes_facts.py` (`config check --live`) and by `gate.py` only when Hermes is already loaded in the process.
- Settings: an invalid value, or an access-control key the gateway would act on (`allow_from`, `allowed_users`, …),
  is an error. Any other unknown key is ignored with a warning (`<key>: unknown key, ignored`; a name that is not
  key-like, e.g. a pasted token, is `<an unknown key of N characters>`); keys the gateway injects are skipped silently.
- Fail closed: no allow-all switch; a corrupt or unsafe registry file raises instead of admitting anyone. Device
  tokens are stored only as hashes and never logged. An unreadable profile scope is itself a gate violation, and the
  adapter refuses (logging only the error type) when the gate check fails in any other way.
- Core modules (the `core-purity` list in `.importlinter`) import no adapter, framework, Hermes package or I/O;
  `make check-imports` enforces it. `generated/` is never edited by hand (`make check-codegen`).
- Error frames never echo input: a fixed `code`/`message` per kind and no chained parser error (`envelope.py`).
- Targets Python 3.11–3.14 (tests run on 3.14, 3.11 planned), the range Hermes declares in `requires-python`:
  `make lint-py` checks 3.11 syntax, `make typecheck-py` the 3.11 stdlib APIs (no plugin module is mypy-ratcheted).
- Unit tests live in `hermes-plugin/tests/` and run without Hermes (`make test-plugin`).
