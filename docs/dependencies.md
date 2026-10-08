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
versions are the ones the pinned test Hermes ships under Python 3.14, pinned here for the dev venv. A Hermes install
on another Python version or commit may ship other patch versions; the plugin must not depend on a patch-level
difference. Which runtime libraries a Hermes install has depends on its extras (ENGINEERING.md §8).

What the plugin code uses **today**: `jsonschema` (config validation, `config.py`) and `PyYAML` (operator CLI only,
`admin.py`). `aiohttp` and `cryptography` are **not imported yet**: they are reserved for the planned transport and
device CA. The plugin's requirement probe (`check_requirements` in `glasses/__init__.py`) already checks that
`aiohttp`, `jsonschema` and `cryptography` are importable: in a Hermes venv without one of them the gateway does not
build the platform and logs `Platform 'Glasses' requirements not met (<install hint>)`. Outside the plugin, `aiohttp`
is imported by the test platform under `tools/test-hermes/`, which runs in the test Hermes venv
([test-hermes.md](test-hermes.md)).

**dev** = test and quality tools only, never imported by shipped code and never installed into the Hermes venv.

| Name | Version | Licence | Scope | Why | Alternative rejected |
|---|---|---|---|---|---|
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | runtime | Planned (not imported yet): WebSocket/HTTP server of the glasses platform adapter | `websockets` — also a Hermes core dependency, but one aiohttp server serves both the WebSocket and the HTTP `/enroll` endpoint |
| jsonschema | 4.26.0 | MIT | runtime | Validates the plugin config (and, in tests, protocol messages) against the JSON Schema 2020-12 files | `fastjsonschema` — not in the Hermes venv; hand-written validators drift from the schema |
| PyYAML | 6.0.3 | MIT | runtime | Reads the Hermes `config.yaml` in the operator CLI and in tests | `ruamel.yaml` — round-trip editing is not needed for reading |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | runtime | Planned (not imported yet): device CA and client certificates (key generation, signing, parsing) | Calling the `openssl` CLI — output parsing and version differences per platform |
| pytest | 9.1.1 | MIT | dev | Test runner for every Python suite (fixtures, parametrize, `tmp_path`) | stdlib `unittest` — more boilerplate, no parametrized fixtures |
| ruff | 0.16.10 | MIT | dev | Lint, format, complexity and size rules in one pinned binary | flake8 + black + isort + mccabe — four tools and four pins for the same rules |
| mypy | 2.4.0 | MIT | dev | Static types, `--strict` | pyright — needs a Node.js runtime in the dev toolchain |
| types-PyYAML | 6.0.12.20260906 | Apache-2.0 | dev | Type stubs so `mypy --strict` checks the code that uses PyYAML | `ignore_missing_imports` for `yaml` — every YAML call would be untyped |
| import-linter | 2.15 | BSD-2-Clause | dev | Import contracts: the core never imports an adapter, framework or I/O library | AST check only — misses indirect imports through other modules |
| coverage | 7.16.2 | Apache-2.0 | dev | Branch coverage and the per-area `--fail-under` gates | stdlib `trace` — no branch coverage, no thresholds |
| pytest-cov | 7.1.0 | MIT | dev | Collects coverage inside the pytest run the suites already use (`--cov`) | Wrapping each pytest call in `coverage run` — a second command line per gate target |
| hypothesis | 6.168.3 | MPL-2.0 | dev | Planned (pinned, no test imports it yet): property-based tests for the envelope codec (shrinks failing inputs) | Hand-written random loops — no shrinking, no reproducible failure database |

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

## Licence exceptions

The project licence is Apache-2.0. Dependencies must carry permissive licences, with the reasoned exceptions below.
The automatic licence check is planned (ENGINEERING.md §10, `licence-check`); until it lands, this is reviewed by hand
when a pin changes.

| Name | Licence | Reason it is allowed |
|---|---|---|
| hypothesis | MPL-2.0 | Test-only: meant for test files (none imports it yet), never imported by shipped code, never vendored or redistributed, not installed into the Hermes venv. MPL-2.0 is file-level copyleft and applies only to modified hypothesis files, which this project does not have. |
| pathspec | MPL-2.0 | Dev-only, transitive (required by mypy): never imported by shipped code, never vendored or redistributed, not installed into the Hermes venv; this project modifies none of its files. |

No other pin in `requirements-dev.txt` carries an MPL, GPL, LGPL or AGPL licence (checked against the installed
metadata and licence files of all 40 pins).
