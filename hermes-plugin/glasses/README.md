<!-- SPDX-License-Identifier: Apache-2.0 -->
# glasses — Hermes gateway platform plugin

## Purpose

The `glasses` platform of the Hermes gateway and its operator CLI: config validation, device registry, allow-all
gate, envelope codec (not called yet), signer client (callers: `crl`, the `/enroll` relay - not mounted until T1.07)
and a fail-closed adapter skeleton (`connect()` fails retryably, `send()` sends nothing). Planned: protocol server.

## Public API

- `register(ctx)` (`__init__.py`; manifest `plugin.yaml`); `adapter.py`, a Hermes `BasePlatformAdapter`: see Purpose.
- `config*.py` + `config.schema.json` — settings under `gateway.platforms.glasses.extra`: `config.load()`, `check()`.
- `gate.py` — allow-all / wildcard gate (`violations()`), read the way the gateway reads it.
- `registry.py` — device registry `devices.json` (`add`, `verify`, `revoke`). No CA key here: the signer signs.
- Envelope codec, core; schema injected (`FrameSchema`; loading it is planned in T1.07): `envelope.py` `decode`,
  `error_frame`; `envelope_scan.py` caps, nesting pre-scan; `envelope_rules.py` `violation`; `envelope_encode.py`
  `encode` (`EncodeRefused`: schema, rules, cap, device-only type); `envelope_text.py` `cut_utf8`, `sanitize_card_text`.
- `signer_protocol.py` (core): `build_request`, `decode_answer`; `signer_client.py` sends them on the signer socket
  (callers: `crl`, `enroll.py`). Errors: `SignerError` → `RequestRefused` / `AnswerRejected` / `SignerRefusal`.
- Operator CLI `config check` (`admin.py`, `hermes_merge.py`, `hermes_facts.py`, `hermes_env.py`; `--live` never writes, see
  `admin.py`), `crl` (`admin_crl.py`), `crl --check` (`crl_check.py`: published CA/CRL, `$state_dir/crl-seen.json`),
  `revoke` (`admin_devices.py`: signer `revoke`, then `$state_dir/admin.sock`, `admin_protocol.py`; a config with
  errors stops it before both steps; server planned, T1.63a); shared `admin_common.py`, `admin_names.py` (names `--live` may print); `python -m glasses.admin <cmd>`.

## Invariants

- Importing the package or calling `register` imports no Hermes module. Hermes is imported by `adapter.py`, by
  `hermes_facts.py`/`hermes_env.py` (`config check --live`) and by `gate.py` only when Hermes is already loaded in the process.
- Settings: an invalid value, or an access-control key the gateway would act on (`allow_from`, `allowed_users`, …),
  is an error. Any other unknown key is ignored with a warning that names it only when it is a key-like near-miss of
  a known key (`hots` → `did you mean host?`), else `<an unknown key of N characters>`; injected keys are silent.
- Fail closed: no allow-all switch; a corrupt or unsafe registry or CRL record raises instead of
  admitting anyone. Device tokens are stored only as hashes and never logged. An unreadable profile scope is itself
  a gate violation, and the adapter refuses (logging only the error type) when the gate check fails in any other way.
- Core modules (the `core-purity` list in `.importlinter`) import no adapter, framework, Hermes package or I/O;
  `make check-imports` enforces it. `generated/` (protocol `TypedDict`s): `tools/codegen/` only, `make check-codegen`.
- Error frames (`envelope.py`) and signer errors (`signer_protocol.py`) never echo input: fixed messages, no chaining.
- Targets Python 3.11–3.14 (Hermes' `requires-python`; tests run on 3.14, 3.11 planned): `make lint-py` checks 3.11
  syntax, `make typecheck-py` 3.11 stdlib APIs (none mypy-ratcheted). `tests/` run without Hermes (`make test-plugin`).
