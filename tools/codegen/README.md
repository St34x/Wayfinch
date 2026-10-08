<!-- SPDX-License-Identifier: Apache-2.0 -->
# Code generation

## Purpose

Generates files from the repository's schemas so nothing is hand-copied (`ENGINEERING.md` section 4): Python
protocol types from the protocol contract, the configuration reference from the plugin config schema, and the
[make-target reference](../../docs/reference/make-targets.md) from the `#:` lines of the `Makefile` and `mk/*.mk`.

## Public API

- `gen_protocol.py [--root DIR] [--out-dir DIR]` — writes `hermes-plugin/glasses/generated/` from
  `protocol/envelope.schema.json` and the "Message types" table of `protocol/README.md` (`FrameType`, one
  `TypedDict` per body and per frame, the `Frame` union, `FRAME_TYPES`, `DEVICE_TO_SERVER`, `SERVER_TO_DEVICE`).
- `check_fresh.sh PYTHON [DIR]` — regenerates the protocol types into a temp dir and `diff -r`s them.
- `gen_config_docs.py [--root DIR] [--out-dir DIR] [--check]` — writes `docs/reference/config.md` from
  `hermes-plugin/glasses/config.schema.json`; `--check` is its freshness test.
- `gen_make_docs.py [--root DIR] [--out-dir DIR] [--check]` — writes `docs/reference/make-targets.md`; `--check`
  (`make check-make-docs`) also checks `#:` lines, the `ENGINEERING.md` §10 gate table and `make <t>` in public docs.
- `protocol_model.py` (schema subset → type model), `protocol_envelope.py` (envelope → model), `make_docs_model.py`
  (make files, gate table, doc commands → findings) — pure library modules.
- `tests/` — generator tests. `make check-codegen` runs both freshness checks and every test except
  `test_gen_make_docs*.py`, which `make check-make-docs` runs.

Run the generator after every schema change and commit its output.

## Invariants

- Stdlib only. The `*_model.py` and `protocol_envelope.py` modules are pure; only the `gen_*.py` entry points read and
  write files. Output is deterministic (same input → same bytes).
- `gen_protocol.py` owns its whole output directory: an extra, missing or edited file there fails `check-codegen`
  (`check_fresh.sh`). `gen_config_docs.py` owns exactly `docs/reference/config.md`, not its directory: a missing,
  edited or stale file, or a symlink in its place, fails `check-codegen` (`--check`). `gen_make_docs.py` owns exactly
  `docs/reference/make-targets.md` in the same way (`check-make-docs`).
- A schema keyword or form a generator cannot represent is an error naming its JSON pointer (exit 2), never
  silently dropped. Narrowing keywords (bounds, patterns) are left to the validator and the codec. Known gap:
  `gen_config_docs.py` renders a mapping whose values carry a schema (`additionalProperties: {...}`, no `properties`)
  as `mapping` without that value schema; a test fails `check-codegen` if the config schema gains such a key.
- Generated files carry an `@generated` header and are never edited by hand.
