<!-- SPDX-License-Identifier: Apache-2.0 -->
# Engineering standards

These rules apply to every change in this repository: the Android app (`android/`), the Hermes plugin
(`hermes-plugin/`), the protocol (`protocol/`), tools and docs. A rule marked _(gate)_ is enforced by a gate, and
section 10 lists every gate with its make target and its state. **`make test` runs every live gate** (CI will run the
same targets once it exists). A gate that is still **planned** (marked _(gate, planned)_ where the whole rule waits for
it, and listed as planned in section 10) is a rule reviewers check by hand until it lands; this applies to every
Kotlin and Android rule, because the Android app is not in the tree yet. Reviewers use this file as their
checklist: a size/complexity violation is a **should-fix**, a dependency-direction (boundary) violation is a
**blocker**.

## 1. Architecture: ports and adapters

```
            ┌────────────── adapters (I/O) ──────────────┐
  BLE / G2 driver   WSS link   Hermes gateway   Android framework   Telegram
            └──────────────▲─────────────────▲───────────┘
                           │  ports (interfaces owned by the core)
            ┌──────────────┴─────────────────┴───────────┐
            │ pure core: HUD composer + scheduler,        │
            │ input reducer, long-answer router decision, │
            │ confirmation rules, envelope codec, i18n    │
            └─────────────────────────────────────────────┘
```

- **Pure core** = deterministic functions and state machines with no I/O: no sockets, files, clocks, threads,
  Android or Hermes imports. Time comes in as a value or an injected `Clock`. Core code is tested without fakes of
  the outside world beyond plain data.
- **Adapters** are thin: translate between the outside world and core types, nothing else. They hold no business
  rules (no "if long then Telegram" in a socket handler).
- **Dependencies point inward** _(gate)_: an adapter may import the core; the core never imports an adapter, a
  framework or an I/O library.
  - Python: **import-linter** contracts in `.importlinter` (core modules ↛ adapter modules, `aiohttp`, Hermes
    packages, stdlib I/O modules — also through other modules) plus `tools/quality/check_core_purity.py`, which
    checks every direct import of a core module against an allowlist and bans builtin I/O, clock reads and console
    streams.
  - Kotlin _(planned, with the Android app)_: **Gradle module boundaries** (core modules `:hud`, `:input`,
    `:protocol` are plain `kotlin("jvm")` modules with no Android or network dependency) plus **ArchUnit** rules for
    package-level direction inside a module.
- **One module = one responsibility.** Every module directory has a `README.md` of at most one screen (≤40 lines)
  with three headings: **Purpose**, **Public API**, **Invariants** _(gate)_.
  - Exception: a specification document (today only `protocol/README.md`) may exceed the limit and add sections
    after the three. It needs one documented override line in `tools/quality/modules.txt`:
    `<dir> | length-override | reason: <why, naming the specification> | ref: ENGINEERING.md §1`.

## 2. Size and complexity limits _(gate)_

| Measure | Target | Hard limit (`make test` fails) |
|---|---|---|
| Source file length | ≤300 lines | 400 lines |
| Function / method length | ≤40 lines | 60 lines |
| Cyclomatic complexity per function | — | 10 |
| Parameters per function | — | 4 (data-class constructors excepted) |
| Nesting depth of blocks | — | 3 |

Targets are printed as warnings; hard limits fail the build. Tools: Python — **ruff** (`C901` max-complexity 10,
`PLR0913`/`PLR0917` max 4 arguments, `PLR1702` max 3 nested blocks) plus `tools/quality/check_size.py` (file and
function lines, all languages); Kotlin _(planned)_ — **detekt** (`CyclomaticComplexMethod`, `LongMethod`,
`LongParameterList`, `NestedBlockDepth`) plus the same size checker.

**Exempt** (listed in `tools/quality/size-exempt.txt`, nothing else): generated files (header `@generated`, paths
`**/generated/**`); JSON Schema and other data (`*.schema.json`, `protocol/fixtures/**`, golden test resources,
`*.json` catalogs, XML string resources); lock files (`requirements-dev.txt`, Gradle `verification-metadata.xml`);
`LICENSE`, `NOTICE`; vendored upstream code under `android/third_party/*/` (only the upstream-shaped files listed in
its `UPSTREAM.md`; new files we add there follow the limits). Test files follow the file and function limits; the
parameter limit does not apply to test functions (pytest injects fixtures and parameters). A function that must mirror
an external signature (a fake of a framework API) may exceed the parameter limit with an inline `noqa` naming the API.

## 3. Atomic changes

- One task = one behaviour, about 20 minutes of work, **≤ ~300 changed lines of non-test, non-generated code**
  (tests and generated files do not count; documentation, build fragments and configuration do).
- **Every commit is green**: `make test` passes at every commit, not only at the branch head.
- Commit messages: `T<id>: <what changed>` for task work (e.g. `T1.06: reject lone surrogates in the codec`);
  other commits use a lower-case prefix (the prefixes for contributors are listed in `CONTRIBUTING.md`; the gate's
  full list is `SUBJECT_PREFIXES` in `tools/quality/check_hygiene.py`) _(gate: `check_hygiene.py --commits` over the
  range after `tools/quality/commit-gate-start.txt`; run at merge time until CI runs it)_.
- **No `TODO`/`FIXME` without a task id**: `TODO(T1.23): …` _(gate)_.
- **No dead code**: no commented-out code (ruff `ERA001`), no unused imports, variables or private members (ruff
  `F401`/`F841`; Kotlin, planned: detekt `UnusedPrivateMember`/`UnusedPrivateProperty`) _(gate)_.

## 4. Single source of truth

- **Protocol**: `protocol/envelope.schema.json` (JSON Schema 2020-12) is the only definition. Types are
  **generated** from it by `tools/codegen/` (stdlib Python, in-tree, versioned with the schema). Built today: Python
  `TypedDict`s + `Literal` type names. Planned, with the Android app: Kotlin `sealed interface Body` + one
  `@Serializable data class` per frame type, from the same generator. Generated files are committed, carry an
  `@generated` header and are never edited by hand; `make check-codegen` regenerates into a temporary directory and
  fails on any difference _(gate)_. Byte limits and delivery rules live in
  `protocol/README.md`.
- **Plugin configuration**: `hermes-plugin/glasses/config.schema.json` drives validation (`jsonschema`) and the
  generated reference `docs/reference/config.md` (freshness checked like codegen) _(gate)_.
- **UI strings** live in catalogs only (planned: no catalog exists yet, because no UI code does): Android
  `res/values*/strings*.xml`, the HUD catalog `android/hud/src/main/resources/i18n/`, plugin catalogs
  `hermes-plugin/glasses/i18n/<locale>.json`. Code refers to message ids, never to literal user-visible text (Android
  lint `HardcodedText` as error; catalog parity tests).
- **Terms**: one concept = one name, defined in `docs/glossary.md`.
- **Names** _(gate, planned: `check-private`)_: no other company's or product's name in our identifiers, paths,
  package, module and class names or configuration keys. Third-party names appear only where law or a licence
  requires them — copyright and licence headers kept verbatim in vendored files, `NOTICE`, `THIRD_PARTY_NOTICES.md`,
  the `UPSTREAM.md` provenance file of vendored code — and in prose that describes compatibility ("works with
  Even Realities G2 glasses"). Vendored code lives under `android/third_party/<name>/` with a neutral folder name and
  is moved into our own package namespace; its `UPSTREAM.md` records the upstream → our package mapping, so upstream
  changes can still be compared and ported.

## 5. Types and static analysis _(gate)_

- **Python**: `mypy --strict` over the plugin package `hermes-plugin/glasses/` and over `tools/` (tool tests
  included), with a **ratchet**: a module listed in a baseline block of `mypy.ini` (`ignore_errors = True`, one block
  per follow-up task) is exempt until that task types it and deletes the block. Today this is one test-only tool (the
  minimal test platform, `tools/test-hermes/glasses_min`); `mypy.ini` is the current list, and new modules never get
  a block. Hermes packages are not installed in the dev venv, so every Hermes import is untyped (`Any`,
  `ignore_missing_imports` in `mypy.ini`); in the plugin, only `adapter.py`, `gate.py` and `hermes_facts.py` import
  Hermes. Small `typing.Protocol`s or typed records type the Hermes objects of the plugin's entry points
  (`register`'s plugin context, the adapter's platform config and inbound event, the gate's Hermes readers). Values
  whose shape the plugin checks itself at run time still come in as `Any` or `object`: the platform config in the
  `validate_config` and `is_connected` hooks, and Hermes' config and tool sets in `hermes_facts.py`
  (`config check --live`). The plugin tests (`hermes-plugin/tests/`) are not type-checked yet: they will be covered
  by a **test profile** — strict, except that test functions may omit annotations (fixtures stay fully typed) —
  ratcheted per file once that follow-up lands. `ruff check` + `ruff format --check` cover all Python code.
- **Kotlin** _(planned, with the Android app)_: **detekt** + **ktlint**; **explicit API mode** in every library
  module; **no `!!`** (detekt `UnsafeCallOnNullableType`); UI and link state as **sealed types**; `val` and
  read-only collections by default — mutable state is private to one class and never exposed.
- No suppression without a reason in the same line; suppressions are counted in review.

## 6. Tests

- **TDD**: a failing test first, then the code.
- **Coverage** _(gate)_: pure core ≥90 %, adapters ≥70 %, statements and branches (Python: coverage.py + pytest-cov
  over the plugin package, per file and per area — every core file ≥90 %, every other plugin file ≥70 %, a file below
  its minimum only through an exact ratchet entry in `.coveragerc`; Kotlin, planned: Kover). The core module list is
  the same list the import contracts use — one list, two checks.
- **Contract tests at every boundary**: protocol fixtures in both stacks, Hermes internal APIs we call, G2 wire bytes
  (golden byte vectors), Telegram hand-off.
- **Property-based tests** for the envelope codec and UTF-8 cutting (Python **Hypothesis**, Kotlin
  **kotest-property**), written with the codec (planned; no property test exists yet).
- **Golden snapshot tests** for HUD layouts (planned, with the HUD): every skeleton × state renders to a 1-bit PNG
  compared with a committed golden file; an intended change updates the golden in the same commit.
- **Fakes over mocks**: `FakeGlasses`, fake GATT, fake clock, mock LLM, fake device. Interaction mocks only where no
  behaviour can be observed.

## 7. Errors and logs

- **Fail closed**: an unknown state, an expired approval, an unreadable config → deny/stop, never "allow".
- **Typed errors**: Python exception classes per failure kind; Kotlin sealed `Result`-style types. No catch-all
  (`except Exception`, `catch (e: Throwable)`) except at the outermost adapter boundary, where the error is logged with
  its type and converted to a typed error (ruff `BLE001`; Kotlin, planned: detekt `TooGenericExceptionCaught`)
  _(gate)_.
- **Structured logs** (key=value / JSON), never secrets, tokens, certificates, audio or full message text; ids and
  sizes instead.

## 8. Dependencies

- Minimal: the standard library first. The plugin installs nothing: its runtime dependencies are only what the Hermes
  venv provides (`docs/dependencies.md`). Today the plugin code imports `jsonschema` (config validation) and `PyYAML`
  (operator CLI only); its requirement probe also checks for `aiohttp` and `cryptography`, which the planned transport
  and device CA will use. A base Hermes install does not provide all of them: `cryptography` is a core dependency of
  the pinned Hermes, but `aiohttp` comes only with Hermes' messaging extras (for example `messaging`), `jsonschema`
  only with its `mcp` extra, and `PyYAML` only through extras that pull it in.
- **Supported Python** (decision): the plugin supports exactly the range Hermes declares in `requires-python`,
  `>=3.11,<3.15`, because it runs inside the Hermes venv. The Hermes commit pinned for the live tests itself needs
  Python 3.14 (`docs/test-hermes.md`); an installed Hermes may run on 3.11 or newer, and the 3.11 floor is there for
  Hermes releases within the declared range. The floor, 3.11, is enforced by the gates _(gate,
  section 10)_: ruff `target-version = "py311"` (syntax added later is `invalid-syntax`; `ruff format` keeps 3.11
  syntax), mypy `python_version = 3.11` (stdlib APIs added later fail) and a 3.11 syntax check of every tracked `*.py`
  in `lint-py` (ruff; generated code and files outside the lint dirs included). Both settings apply to `tools/` too:
  one floor for all Python code. Exception: a module under a ratchet block of `mypy.ini` (`ignore_errors = True`, one
  block per follow-up task id) is not API-checked against 3.11 until its task removes the block; its syntax is still
  checked. The dev and test toolchain uses Python 3.14 (`make venv`), so the test suites run on 3.14 only today;
  running the plugin test suite under Python 3.11 is planned.
- **Pinned with hashes**: Python `requirements-dev.txt` installed with `pip --require-hashes`; the gitleaks binary is
  pinned by version and SHA-256 (`tools/secret-scan.sh`). Planned, with the Android build: Gradle **dependency
  verification** (`gradle/verification-metadata.xml`, sha256) and ktlint and detekt pinned by version and checksum.
- **Licence check** _(gate, planned)_: an allowlist with reasoned exceptions only (`docs/dependencies.md`); reviewed
  by hand until the gate lands.
- **Each new direct dependency is justified** by one line in `docs/dependencies.md` (name, version, licence, why,
  alternative rejected) _(gate, planned: the ledger must list every direct dependency)_.

## 9. Documentation (docs as code)

- **Docs change with behaviour, in the same commit.** A change to a command, make target, CLI subcommand, config
  key, default, limit, protocol field, file path or user-visible behaviour updates every document that states it in
  the same commit. Missing or stale documentation is a **should-fix** review finding (changes requested), never a
  later "docs task".
- **Generate references, do not copy them.** Where a source of truth exists, the reference is generated from it and
  checked for freshness: configuration from `hermes-plugin/glasses/config.schema.json` (`docs/reference/config.md`),
  protocol types from `protocol/envelope.schema.json`, make targets from `mk/*.mk`, CLI subcommands from the CLI
  parser. Hand-written docs link to the reference instead of repeating its values.
- **Docs are tested** _(gate, planned; section 10)_: internal links and anchors resolve, every document is reachable
  from an index, every make target, script and path a document names exists, and commands marked as doc tests run.
- **Self-contained public tree**: the team's working notes stay outside the published repository, so no file may
  point at them: no path into the team folder, no decision-record number, no QA or review report name and no
  review-process tag. Matching is case-insensitive and covers the whole text of every tracked text file outside the
  team folder and every tracked path name: the folder by its dot-prefixed name anywhere in a line; a decision-record
  number as `adr` at the start of a word, up to two separators (`-`, `_`, `/`, `#` or a space) and digits; a report
  name as `qa` or `review`, then `-` or `_`, then a task id. Review-process tags, where a separator is an optional
  `-`, `_` or space: a review, QA or fix round number (`review`, `qa` or `fix`, a separator, then `round` or `rounds`
  plus a separator, or just `r`, then a digit); a QA wave or finding id (`qa`, a separator, `w` or `s`, a digit); a
  wave number (`wave`, a separator, one or two digits, an optional letter); `round`, a separator, a number, then
  `-`, `_` or a space and `fix`, `fixes`, `finding(s)` or `xfail(s)`; and a list or decision of the team lead
  (`lead`, `lead's` or `leads`, then `-`, `_` or a space and `list` or `decision(s)`). A tag must not directly
  follow a letter or digit (it may follow `_`), and a wave number, `round` phrase or lead phrase must not run on into
  one, so look-alikes such as a round-trip, a sine wave, `noqa` or a prefix pass. A file name with a `_qa` or `-qa`
  suffix (optionally with digits) right before its extension is a finding too. Cite the public document that carries
  the reasoning (this file, `docs/`, `protocol/README.md`) or explain it in one sentence where it is needed. Task ids
  may stay. Only the gate's own files, listed in `PRIVATE_FOLDER_EXEMPT` of `check_hygiene.py`, may name the folder,
  because they must name it to exclude it; they are still checked for record numbers, report names and
  review-process tags _(gate: `check-hygiene`)_.
- One topic per file. Design decisions: at most two pages each.
- `docs/glossary.md`: one term = one name.
- `docs/troubleshooting/`: one file per solved problem (Symptom with the exact error text, Context, Cause, Fix,
  Verified, Related) and an `INDEX.md`. Search it before debugging; add an entry when you solve something new.

## 10. Gates

Every _(gate)_ above, the make target that enforces it and its state. `make test` runs every live target; a planned
row names the intended target, which does not exist yet or does not check that rule yet; a row
changes from planned to live in the same commit that wires its target into `make test`. A gate that lands on existing
code starts with a **ratchet baseline** (`tools/quality/size-baseline.txt`, the baseline blocks in `ruff.toml` and
`mypy.ini`): today's violations are listed in one block per follow-up task id, so the gate is green; a new or grown
violation fails, and the task that fixes a file deletes its own block. New code never gets an entry.

| Rule | Make target | State |
|---|---|---|
| No secrets in the tree or history | `secret-scan` | live |
| SPDX licence header on every source file | `check-spdx` | live |
| File and function length (section 2) | `check-size` | live |
| Lint, format, complexity, parameters, nesting, dead code, catch-all (sections 2, 3, 7) | `lint-py` | live |
| Static types, `mypy --strict` (section 5) | `typecheck-py` | live |
| Dependencies point inward, Python (section 1) | `check-imports` | live |
| Generated protocol types are fresh (section 4) | `check-codegen` | live |
| `TODO` with a task id, glossary terms unique, no private references (sections 3, 9) | `check-hygiene` | live |
| Commit subjects (section 3) | `check_hygiene.py --commits` | live at merge time; CI planned |
| Module READMEs: Purpose / Public API / Invariants, ≤40 lines (section 1) | `check-hygiene` | live |
| Configuration reference is fresh (sections 4, 9) | `check-codegen` | live |
| Coverage: plugin core files ≥90 %, other plugin files ≥70 %, exact ratchet entries below that (section 6) | `coverage-py` | live |
| Python code parses (every tracked `*.py`) and type-checks for the oldest Python that Hermes declares in `requires-python`, except modules under a mypy ratchet block (section 8) | `lint-py`, `typecheck-py` | live |
| Docs: internal links and anchors resolve, every document is indexed (section 9) | `check-docs` | planned |
| Docs: make-target reference and this table match `mk/*.mk` (section 9) | `check-make-docs` | planned |
| Dependency ledger lists exactly the direct dependencies (section 8) | `check-ledger` | planned |
| CLI reference is fresh (section 9) | `check-codegen` | planned |
| Commands in docs exist; marked doc tests run (section 9) | `test-docs` | planned |
| No private deployment identifiers in the tree; no third-party names in identifiers and paths (section 4) | `check-private` | planned |
| Make fragments follow `mk/README.md` | `check-mk-fragments` | planned |
| Licences on the allowlist; generated third-party notices | `licence-check` | planned |
| Kotlin: detekt, ktlint, module boundaries, ArchUnit, Kover, generated Kotlin types | `test-android` | planned |

## Pinned toolchain

| Tool | Version | Use |
|---|---|---|
| ruff | 0.16.10 | Python lint, format, complexity/size rules |
| mypy | 2.4.0 | Python static types (`--strict`) |
| import-linter | 2.15 | Python dependency direction |
| coverage / pytest-cov | 7.16.2 / 7.1.0 | Python coverage gates |
| hypothesis | 6.168.3 | Python property tests (test-only; pinned, not used by any test yet) |
| detekt-cli | 1.23.8 | Kotlin static analysis and complexity (planned: chosen version, not in the tree yet) |
| ktlint-cli | 1.8.0 | Kotlin formatting (planned: chosen version, not in the tree yet) |
| ArchUnit | 1.5.1 | Kotlin/JVM architecture rules (planned: chosen version, not in the tree yet) |
| Kover | 0.9.11 | Kotlin coverage gates (planned: chosen version, not in the tree yet) |
| kotest-property | 6.2.5 | Kotlin property tests, test-only (planned: chosen version, not in the tree yet) |
| `tools/codegen/` | in-tree | schema → Python types (Kotlin types planned), config schema → docs |

Upgrading a pin is its own change with its own task id.
