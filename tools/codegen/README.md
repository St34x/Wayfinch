<!-- SPDX-License-Identifier: Apache-2.0 -->
# Code generation

## Purpose
Generates files from the repository's schemas so nothing is hand-copied (`ENGINEERING.md` section 4): Python and
Kotlin protocol types, the configuration reference and the [make-target reference](../../docs/reference/make-targets.md).

## Public API
- `gen_protocol.py [--root DIR] [--out-dir DIR]` — writes `hermes-plugin/glasses/generated/` from
  `protocol/envelope.schema.json` and the "Message types" table of `protocol/README.md` (`FrameType`, one
  `TypedDict` per body and per frame, the `Frame` union, `FRAME_TYPES`, `DEVICE_TO_SERVER`, `SERVER_TO_DEVICE`) in
  `protocol_types.py`, plus `__init__.py` and `envelope.schema.json`, a byte copy of the schema that the adapter loads.
- `check_fresh.sh PYTHON [DIR]` — regenerates the types and the schema copy into a temp dir and `diff -r`s DIR.
- `regenerate.sh [PYTHON]` — after a protocol schema edit: `gen_protocol.py`, then `gen_kotlin.py`, in one command.
- `gen_kotlin.py [--root DIR] [--out-dir DIR] [--check]` — writes `Envelope.kt` of the Android module `:protocol`
  (`android/protocol/src/main/kotlin/app/hudcompanion/protocol/generated/`) from the same model: `sealed interface
  Body`, one `@Serializable` data class per body, enums, `Envelope` and the strict `decodeEnvelope`; `--check`: freshness.
- `gen_config_docs.py [--root DIR] [--out-dir DIR] [--check]` — writes `docs/reference/config.md` from the plugin
  config schema `hermes-plugin/glasses/config.schema.json`; `--check` is its freshness test.
- `gen_make_docs.py [--root DIR] [--out-dir DIR] [--check]` — writes `docs/reference/make-targets.md`; `--check`
  (`make check-make-docs`) also checks `#:` lines, the `ENGINEERING.md` §10 gate table and `make <t>` in public docs.
- Pure library modules: `protocol_model.py`, `protocol_envelope.py` (schema → type model), `kotlin_emitter.py` +
  `kotlin_runtime.py` (model → Kotlin text; the strict-decoder code and design), `make_docs_model.py` (make docs).
- `tests/` — generator tests (`make check-codegen`). Compiling `Envelope.kt` and decoding the fixtures with it runs on a
  machine with a JDK and the Android SDK (`make test-android-jvm`), not in `make test`.

## Invariants
- Stdlib only. The `*_model.py`, `protocol_envelope.py` and `kotlin_emitter.py` modules are pure; only the `gen_*.py`
  entry points read and write files. Output is deterministic (same input → same bytes).
- `gen_protocol.py` and `gen_kotlin.py` own their whole output directory: an extra (hand-written), missing or edited
  file there fails `check-codegen` (`check_fresh.sh`, `--check`). `gen_config_docs.py` owns exactly
  `docs/reference/config.md`, `gen_make_docs.py` exactly `docs/reference/make-targets.md` (`check-make-docs`): a
  missing, edited or stale file, or a symlink in its place, fails the check.
- A schema keyword or form a generator cannot represent is an error naming its JSON pointer (exit 2), never silently
  dropped. Narrowing keywords are left to the validator and the codec, except integer bounds (the Kotlin decoder
  enforces them). Known gap: `gen_config_docs.py` renders a mapping whose values carry a schema
  (`additionalProperties: {...}`, no `properties`) as `mapping` without it; `check-codegen` fails if the config schema
  gains such a key.
- Never hand-edit generated files. Markers: `@generated` (`protocol_types.py`, `Envelope.kt`), "Code generated"
  (`__init__.py`), none (schema).
