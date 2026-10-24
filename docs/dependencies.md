<!-- SPDX-License-Identifier: Apache-2.0 -->
# Dependencies

The ledger of every **direct** dependency: one row each, with the reason it is here and the alternative that was
rejected. A change that adds, removes or upgrades a direct dependency updates its row in the same commit
(ENGINEERING.md §8); a new one also needs a `# direct: <name> (<scope>)` line in `requirements-dev.txt` and the
"all N pins" sentence below updated (`make check-ledger` checks it). Transitive dependencies are not listed here;
they are pinned with their SHA-256 digests in
`requirements-dev.txt` (`make venv` installs them with `pip --require-hashes --only-binary :all:`, recipe:
[`troubleshooting/pip-require-hashes-missing-hash.md`](troubleshooting/pip-require-hashes-missing-hash.md)).
`--only-binary` keeps pip from building an sdist (its build backend would be installed without a hash check,
[`troubleshooting/pip-tampered-wheel-hash-installs-sdist.md`](troubleshooting/pip-tampered-wheel-hash-installs-sdist.md)).
Every pin has a CPython 3.14 wheel for Linux x86_64/aarch64 and macOS arm64; cryptography 50.0.1 has none for macOS
x86_64, so `make venv` fails there.

Licences are given as SPDX identifiers of each package's declared licence.

## Python

Scope: **runtime** = a library the Hermes plugin uses from the Hermes venv. The plugin installs nothing, so these
versions are the ones the pinned test Hermes ships under Python 3.14 (the same at Hermes `d923a5eb48` as at the
previous pin `a4648c5f58`: aiohttp 3.14.3, jsonschema 4.26.0, PyYAML 6.0.3, cryptography 50.0.1), pinned here for the dev venv. A Hermes install
on another Python version or commit may ship other patch versions; the plugin must not depend on a patch-level
difference. Which runtime libraries a Hermes install has depends on its extras (ENGINEERING.md §8).

What the plugin code uses **today**: `jsonschema` (config validation, `config.py`) and `PyYAML` (operator CLI only,
`admin_common.py`) and `cryptography` (parsing and verifying the signer's published CA and CRL bundles, `crl_check.py`, and
verifying the forwarded device certificate of each `/ws` upgrade against that CA bundle, `edge_cert.py`; the
CLI imports it only inside `crl --check`, so `config check` and `crl` run without it) and `aiohttp` (the `/enroll`
relay, `enroll.py`; built, mounted for every method by `server.py`, T1.07c, and served on the edge's unix socket). The plugin's requirement probe (`check_requirements` in `glasses/__init__.py`) already checks that
`aiohttp`, `jsonschema` and `cryptography` are importable: in a Hermes venv without one of them the gateway does not
build the platform and logs `Platform 'Glasses' requirements not met (<install hint>)`. Outside the plugin, `aiohttp`
is imported by the test platform under `tools/test-hermes/`, which runs in the test Hermes venv
([test-hermes.md](test-hermes.md)).

**dev** = test and quality tools only, never imported by shipped code and never installed into the Hermes venv.

| Name | Version | Licence | Scope | Why | Alternative rejected |
|---|---|---|---|---|---|
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | runtime | HTTP server of the glasses platform adapter: imported by the `/enroll` relay (`enroll.py`); the WebSocket session (`server_session.py`, run by `server.py`), the connection table (`connections.py`) and the `/enroll` mount (`server.py`; T1.07b-c) are built and served on the edge's unix socket by `edge_listener.py` (T1.184b). `server_runner.py` relies on aiohttp internals to keep a credential out of parse-error bodies and logs (`AppRunner._make_server`, `Server._loop` / `_kwargs`, `RequestHandler.handle_error`, a `__class__` swap; from T1.239 also `RequestHandler.log_exception`, the handler constructor and the handler's `_parser` slot with a callable `feed_data`, which a stand-in replaces; `server_runner.py`): `build_runner` and the runner's `setup` check them at start and raise `AiohttpDrift` for any other major.minor than the tested one (3.14, constant `TESTED_AIOHTTP`, equal to this pin's major.minor by test), a missing internal or a swap not applied. A bump of the minor = re-run `test_server_runner_drift.py`, `test_server_runner_drift_edges.py`, `test_server_peer_and_log.py`, `test_server_refused_request_lines.py`, `test_server_pipelined_bytes_during_auth.py` and `test_server_upgrade_read_pause_exits.py` (the read pause in `server.py` relies on how `RequestHandler.data_received` keeps the bytes behind an upgrade request) and, from T1.239, `test_server_pipelined_tail_logs_by_type_only.py`, `test_server_parser_value_error_logs_by_type_only.py`, `test_server_parser_refusals_in_more_places.py`, `test_server_parser_stand_in_keeps_valid_traffic.py` on the new version, then change the constant and this row in one commit | `websockets` — also a Hermes core dependency, but one aiohttp server serves both the WebSocket and the HTTP `/enroll` endpoint |
| jsonschema | 4.26.0 | MIT | runtime | Validates the plugin config (and, in tests, protocol messages) against the JSON Schema 2020-12 files | `fastjsonschema` — not in the Hermes venv; hand-written validators drift from the schema |
| PyYAML | 6.0.3 | MIT | runtime | Reads the Hermes `config.yaml` in the operator CLI and in tests | `ruamel.yaml` — round-trip editing is not needed for reading |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | runtime | Parsing and verifying the signer's published device CA and CRL bundles (`crl --check`): `crl_check.py`; verifying the forwarded device certificate against the CA bundle: `edge_cert.py`; the plugin holds no CA key | Calling the `openssl` CLI — output parsing and version differences per platform |
| pytest | 9.1.1 | MIT | dev | Test runner for every Python suite (fixtures, parametrize, `tmp_path`) | stdlib `unittest` — more boilerplate, no parametrized fixtures |
| ruff | 0.16.10 | MIT | dev | Lint, format, complexity and size rules in one pinned binary | flake8 + black + isort + mccabe — four tools and four pins for the same rules |
| mypy | 2.4.0 | MIT | dev | Static types, `--strict` | pyright — needs a Node.js runtime in the dev toolchain |
| types-PyYAML | 6.0.12.20260906 | Apache-2.0 | dev | Type stubs so `mypy --strict` checks the code that uses PyYAML | `ignore_missing_imports` for `yaml` — every YAML call would be untyped |
| import-linter | 2.15 | BSD-2-Clause | dev | Import contracts: the core never imports an adapter, framework or I/O library | AST check only — misses indirect imports through other modules |
| coverage | 7.16.2 | Apache-2.0 | dev | Branch coverage and the per-area `--fail-under` gates | stdlib `trace` — no branch coverage, no thresholds |
| pytest-cov | 7.1.0 | MIT | dev | Collects coverage inside the pytest run the suites already use (`--cov`) | Wrapping each pytest call in `coverage run` — a second command line per gate target |
| hypothesis | 6.168.3 | MPL-2.0 | dev | Property-based tests of the envelope codec, UTF-8 cutting and the signer protocol (`hermes-plugin/tests/test_envelope_text.py`, `test_envelope_decode.py`, `test_envelope_decode_properties.py`, `test_signer_protocol_properties.py`; shrinks failing inputs) | Hand-written random loops — no shrinking, no reproducible failure database |

## Tool binaries

`tools/secret-scan.sh` is the source of truth: it downloads the release archive and checks it against these
SHA-256 values on every run. The script and this table change in the same commit; an upgrade is its own task.
Source of the digests: `https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt`.

| Name | Version | Licence | Scope | Why | Alternative rejected |
|---|---|---|---|---|---|
| gitleaks | 8.30.1 | MIT | dev | Secret scan of the working tree and the history of HEAD (`make secret-scan`), one static binary | trufflehog — larger, AGPL-3.0 |

| Platform | Archive SHA-256 |
|---|---|
| linux_x64 | `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` |
| linux_arm64 | `e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080` |
| darwin_x64 | `dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709` |
| darwin_arm64 | `b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5` |

## Kotlin and Gradle

The Android app (`android/`, T1.17a). Gradle fetches the artifacts from Maven Central and Google Maven; every artifact,
direct or transitive, is pinned by SHA-256 in `android/gradle/verification-metadata.xml`
and locked in the `gradle.lockfile` files (`make check-ledger` does not read this section yet: the gate
covers the Python pins only). Versions: `android/gradle/libs.versions.toml`. Scope: **build** = runs the build only,
**runtime** = shipped in the app, **test** = `testImplementation` only (never shipped).

| Name | Version | Licence | Scope | Why | Alternative rejected |
|---|---|---|---|---|---|
| Gradle (wrapper) | 9.8.0 | Apache-2.0 | build | Build tool; the wrapper is pinned by the distribution SHA-256 and the SHA-256 of its four files (`make check-android-wrapper`) | A system Gradle — an unpinned version per machine |
| Android Gradle plugin | 9.4.1 | Apache-2.0 | build | Builds the Android modules (T1.17b); loaded only without `-PjvmOnly=true`; its built-in Kotlin fixes the Kotlin version | — |
| Kotlin Gradle plugin | 2.2.10 | Apache-2.0 | build | `kotlin("jvm")` for the pure modules, the same version as the built-in Kotlin of the Android Gradle plugin 9.4.1 (its POM depends on kotlin-gradle-plugin 2.2.10); one version avoids two compilers in one build | A newer Kotlin than the Android Gradle plugin's — mixed compilers |
| Kotlin serialization plugin | 2.2.10 | Apache-2.0 | build | Compiler plugin for the `:protocol` types (T1.105), same version as Kotlin | Hand-written JSON codecs |
| kotlinx-serialization-json | 1.9.0 | Apache-2.0 | runtime | JSON runtime of `:protocol` (the only module with a runtime library), the release built for Kotlin 2.2 | Moshi, Gson — reflection-based, no Kotlin compiler plugin |
| JUnit (Jupiter, platform launcher) | 5.13.4 | EPL-2.0 | test | Test runner of the Kotlin modules. EPL-2.0 is file-level copyleft; it applies only to modified JUnit files, which this project does not have, and the test libraries are never shipped (`testImplementation` only) | kotlin-test alone — still needs a JUnit engine |
| Kotlin standard library (`kotlin-stdlib`) | 2.2.10 | Apache-2.0 | runtime | Added to every module's `runtimeClasspath` by the Kotlin plugin (not declared in any build file), so it ships with the app; its only dependency is `org.jetbrains:annotations` 13.0 (Apache-2.0, also runtime). Pinned in each module's `gradle.lockfile` and in `verification-metadata.xml` | None: the Kotlin compiler requires it |
| detekt-cli | 1.23.8 | Apache-2.0 (not verified against the artifact) | build | Kotlin static analysis (T1.107). Resolved as the self-contained `detekt-cli-1.23.8-all.jar` and run by a `JavaExec` task, so it keeps its own Kotlin compiler and is not on the build classpath; its type resolution reads only `kotlin-stdlib` 2.2.10 (the pin above). Locked in `android/gradle.lockfile`; the jar and `.module` SHA-256 values are pinned in `android/gradle/verification-metadata.xml` (generated by Gradle, not compared with Maven Central by hand) | The detekt Gradle plugin — it loads detekt's compiler into the build classpath |
| ktlint-cli | 1.8.0 | MIT (not verified against the artifact) | build | Kotlin style and formatting (T1.107), same way as detekt: `ktlint-cli-1.8.0-all.jar` run by `JavaExec`, locked in `android/gradle.lockfile`; jar and `.module` SHA-256 values are pinned in `android/gradle/verification-metadata.xml` (generated by Gradle, not compared with Maven Central by hand) | A Gradle ktlint plugin (Spotless and others) — one more plugin on the build classpath for what a jar does |
| Kotlin Compose compiler plugin | 2.2.10 | Apache-2.0 (not verified against the artifact) | build | Compiler plugin for the Compose modules `:app` and `:shell` (T1.17b), the same version as Kotlin | Hand-picked compiler versions — two compilers in one build |
| AndroidX core-ktx | 1.16.0 | Apache-2.0 (not verified against the artifact) | runtime | Kotlin extensions of `androidx.core` in `:app` and `:shell`; pulls `core`, `annotation`, `collection`, `lifecycle-runtime`, `versionedparcelable`, `concurrent-futures` | Platform framework classes only — no compat helpers |
| AndroidX activity-compose | 1.10.1 | Apache-2.0 (not verified against the artifact) | runtime | `ComponentActivity` and `setContent` for the Compose screens of `:app` and `:shell`; pulls `activity`, `activity-ktx`, `savedstate`, `lifecycle-*` 2.9.0 | The View system with XML layouts — the phone screens are Compose |
| Compose BOM | 2025.05.00 | Apache-2.0 (not verified against the artifact) | runtime | Fixes the versions of the Compose artifacts in one place: `runtime`, `ui`, `foundation`, `animation` 1.8.1, `material-icons-core` 1.7.8 (all `-android` variants in the app) | Per-artifact versions in the catalog — they drift apart |
| Compose Material 3 (`material3`) | 1.3.2 (set by the BOM) | Apache-2.0 (not verified against the artifact) | runtime | Material 3 components of the phone UI (`:app`, `:shell`) | Own components on `foundation` — more code to review |
| AndroidX navigation-compose | 2.9.0 | Apache-2.0 (not verified against the artifact) | runtime | Navigation between the phone screens of `:shell`; pulls `navigation-runtime` / `navigation-common` 2.9.0 and `lifecycle-viewmodel-compose` | A hand-written screen state machine — no back-stack handling |
| AndroidX lifecycle (`lifecycle-runtime`, `viewmodel`, `viewmodel-savedstate`, `process`, `livedata-core`) | 2.9.0 | Apache-2.0 (not verified against the artifact) | runtime | Transitive (activity, navigation, Compose); no build file declares it. The lint and test classpaths also resolve 2.8.7 (from the Android Gradle plugin's own closures); the release runtime classpath has 2.9.0 only. `lifecycle-process` is on it but never starts (no `androidx.startup`, see the next row) | None: required by activity-compose and navigation-compose |
| Other AndroidX libraries (`savedstate` 1.3.0, `startup-runtime` 1.1.1, `profileinstaller` 1.4.0, `tracing` 1.2.0, `collection` 1.5.0, `annotation` 1.9.1, `emoji2` 1.4.0, `core-viewtree` 1.0.0, `graphics-path` 1.0.1, `arch.core` 2.2.0, `versionedparcelable` 1.1.1, `concurrent-futures` 1.1.0, `autofill` 1.0.0, `customview-poolingcontainer` 1.0.0, `interpolator` 1.0.0) | as listed | Apache-2.0 (not verified against the artifact) | runtime | Transitive of core-ktx, activity-compose, Compose and navigation; the exact set is in `android/app/gradle.lockfile`. The manifest parts of `startup-runtime` (`InitializationProvider`) and `profileinstaller` (`ProfileInstallReceiver`) are removed in the `:app` manifest (SAT-PL-01, -02), so `ProcessLifecycleOwner`, EmojiCompat and ProfileInstaller never start | None: pulled in by the rows above |
| kotlinx-coroutines (`-android`, `-core`, `-core-jvm`, `-bom`) | 1.10.2 | Apache-2.0 (not verified against the artifact) | runtime | Coroutines for `:app`, `:hermeslink`, `:glasses` and `:shell` | RxJava, plain threads — callback code without structured cancellation |
| kotlin-stdlib-common, kotlin-stdlib-jdk7, kotlin-stdlib-jdk8 | 2.2.10, 1.9.10, 1.9.10 | Apache-2.0 (not verified against the artifact) | runtime | Transitive in the `:app` release runtime classpath (since Kotlin 1.8 the jdk7/jdk8 artifacts only forward to `kotlin-stdlib`); `org.jetbrains:annotations` there is 23.0.0 | None: pulled in by the OkHttp and kotlinx closures |
| OkHttp | 4.12.0 | Apache-2.0 (not verified against the artifact) | runtime | The phone's HTTP and WebSocket client in `:hermeslink` (enrolment POST, `/ws` session; certificate pinning to the CA set lives in T1.24a) | `HttpURLConnection` — no WebSocket client and no certificate-pinning hooks |
| Okio (`okio`, `okio-jvm`) | 3.6.0 | Apache-2.0 (not verified against the artifact) | runtime | Transitive of OkHttp 4.12.0 (I/O buffers) | None: OkHttp requires it |
| Small transitive libraries (`listenablefuture` 1.0, `org.jspecify:jspecify` 1.0.0, `org.jetbrains:annotations` 23.0.0) | as listed | Apache-2.0 (not verified against the artifact) | runtime | Annotation and interface artifacts pulled in by AndroidX and Compose; `jspecify` and `listenablefuture` appear in the `:shell` lock file (`javax.inject` 1 remains only in the lint tool's build classpath) | None: pulled in by the rows above |
| MockWebServer, okhttp-tls | 4.12.0 | Apache-2.0 (not verified against the artifact) | test | Local HTTPS/WebSocket server and test certificates for the `:hermeslink` and `:app` tests (`testImplementation` and `androidTestImplementation` only, never shipped) | A hand-written fake server — no TLS test certificates |
| kotlinx-coroutines-test (+ `-jvm`) | 1.10.2 | Apache-2.0 (not verified against the artifact) | test | Virtual time for the coroutine tests of `:hermeslink` (`testImplementation` only) | `runBlocking` with real delays — slow and flaky |
| AndroidX Test (`runner` 1.6.2, `ext:junit` 1.2.1; pulls `test:core` 1.6.1, `test:monitor` 1.7.2, `test.services:storage` 1.5.0) | as listed | Apache-2.0 (not verified against the artifact) | test | Instrumented-test runner on the API 36 emulator (`androidTestImplementation` only, `testInstrumentationRunner` of `:app` and `:hermeslink`) | Robolectric — the instrumented tests target the emulator the project builds for |
| JUnit 4 | 4.13.2 | EPL-1.0 (as published by the project; not verified against the artifact) | test | Transitive of MockWebServer and AndroidX Test (also on the unit-test classpaths); the project's own tests use JUnit 5. EPL-1.0 is file-level copyleft; exception below | None: required by AndroidX Test |
| Hamcrest core | 1.3 | BSD-3-Clause (as published by the project; not verified against the artifact) | test | Transitive of JUnit 4 | None: required by JUnit 4 |


### Trust in the pinned checksums

`verify-signatures` is **off** in `android/gradle/verification-metadata.xml`: the SHA-256 values were generated by Gradle
from the first download over HTTPS from Maven Central and Google Maven. That
is trust on first use. Verification catches an artifact or a metadata file that changes **after** that download (a
tampered mirror, a re-published version, a changed dependency graph); it does not catch one that was already bad at the
first download. Twelve artifacts, a mix of Maven Central and Google Maven ones from the Android Gradle plugin, Kotlin,
JUnit and kotlinx closures, were compared with the digests the repositories publish (or with a fresh download checked
against the published SHA-1) and all matched. Residual risk: a compromised publisher or repository at the moment a
version was first pinned. PGP verification with pinned keys would narrow it, but it needs a trusted key per publisher
and not every artifact is known to be signed, so it is not enabled; revisiting it is a separate decision. When a
dependency is added or upgraded, regenerate the metadata and compare the new digests with the published ones where the
repository publishes them. The 393 components of the metadata file include the closures T1.17b added (AndroidX,
Compose, OkHttp, kotlinx-coroutines); they were generated the same way and this ledger records no comparison of them
with published digests.

### Licences of the Kotlin and Gradle dependencies

The project rule (permissive licences, reasoned exceptions) applies here as well. The `check-ledger` gate and the
planned `licence-check` gate read only the Python tables above, so this section is reviewed by hand.

| Name | Licence | Scope | Reason it is allowed |
|---|---|---|---|
| JUnit (Jupiter, platform launcher) | EPL-2.0 | test | Test-only: `testImplementation` only, so never part of the app, never vendored or redistributed. EPL-2.0 is weak (file-level) copyleft and applies only to modified JUnit files, which this project does not have. |
| JUnit 4 | EPL-1.0 | test | Test-only and transitive (MockWebServer, AndroidX Test): never part of the app, never vendored or redistributed. EPL-1.0 is weak (file-level) copyleft and applies only to modified JUnit files, which this project does not have. Licence as published by the project; not verified against the artifact. |

The Android Gradle plugin and its dependencies (115 locked artifacts in `android/buildscript-gradle.lockfile`, together
with the Kotlin plugins: the `com.android.tools*` libraries, Guava, Protobuf, Bouncy Castle, JNA, the JAXB runtime, ASM
and others), and the detekt-cli and ktlint-cli jars (which bundle a Kotlin compiler, IntelliJ core classes and their own
libraries), are **build-only**: they run inside the Gradle build and are not packaged into the app. Their licences are
**not yet reviewed** one by one. Some are known to differ from Apache-2.0, for example JNA (dual LGPL-2.1 / Apache-2.0)
and the JAXB runtime (EDL/BSD); this is stated from the projects' published licences and was not checked against the
artifacts. Because build tooling is not part of the distributed app, these are not listed as exceptions above; the
review and an allowlist for them belong to the `licence-check` gate (ENGINEERING.md §10) and must exist before the
first release.

### Removed: the Google code scanner

`play-services-code-scanner` 16.1.0 was declared by T1.17b for the QR way of enrolment and removed again: it brought
proprietary Play services and ML Kit libraries, Google's
datatransport telemetry pipeline and the `ACCESS_NETWORK_STATE` permission into the app, and the enrolment message
arrives on the same phone, which cannot scan its own screen. Enrolment takes the app link or a paste (T1.23). The
lock files and the verification metadata were regenerated: 35 coordinates left `android/app/gradle.lockfile` (Play
services, ML Kit, Firebase components, datatransport, `odml:image`, and the AndroidX 1.0.0 support libraries and
`lifecycle-livedata` / `lifecycle-livedata-core-ktx` only they pulled in) and 39 components left the metadata (the same
plus four POM-only entries of versions only that closure asked for). No Google Play services library is in the app
now; a later QR scanner is a new task with its own `CAMERA` decision.

## Licence exceptions

The project licence is Apache-2.0. Dependencies must carry permissive licences, with the reasoned exceptions below.
The automatic licence check is planned (ENGINEERING.md §10, `licence-check`); until it lands, this is reviewed by hand
when a pin changes.

| Name | Licence | Reason it is allowed |
|---|---|---|
| hypothesis | MPL-2.0 | Test-only: imported only by test files under `hermes-plugin/tests/`, never by shipped code, never vendored or redistributed, not installed into the Hermes venv. MPL-2.0 is file-level copyleft and applies only to modified hypothesis files, which this project does not have. |
| pathspec | MPL-2.0 | Dev-only, transitive (required by mypy): never imported by shipped code, never vendored or redistributed, not installed into the Hermes venv; this project modifies none of its files. |

No other pin in `requirements-dev.txt` carries an MPL, GPL, LGPL or AGPL licence (checked against the installed
metadata and licence files of all 40 pins). The Kotlin and Gradle dependencies have their own licence section above
([Licences of the Kotlin and Gradle dependencies](#licences-of-the-kotlin-and-gradle-dependencies)).
