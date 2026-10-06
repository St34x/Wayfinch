<!-- SPDX-License-Identifier: Apache-2.0 -->
# Developer tools

## Purpose

Scripts that build, check and test the repository. Nothing here is installed or shipped; the product lives in
`hermes-plugin/`, `protocol/` and `android/`.

## Public API

- `secret-scan.sh` — pinned, checksum-verified gitleaks over the tree and history (`make secret-scan`).
- `check-spdx.sh` — every tracked source file carries the SPDX header (`make check-spdx`).
- `quality/` — the quality gates (size, hygiene, module READMEs, lint, types, imports, coverage); own README.
- `codegen/` — generators: protocol types and the config reference; own README.
- `mock-llm/` — scripted OpenAI-compatible mock LLM for the test Hermes; own README.
- `test-hermes/` — isolated test Hermes: `setup_home.sh`, `run_gateway.sh`, `stop_pidfile.sh`, a minimal plugin
  `glasses_min/` and the hermetic `test_harness.sh` (`make test-hermes-up` / `-down` / `-harness`, see
  `mk/test-hermes.mk`; pinned clone and venv setup: `docs/test-hermes.md`).
- `tests/` — shell tests of the scripts above. `make test` runs `test_secret_scan.sh`, `test_secret_scan_edges.sh`,
  `test_make_fragments_edges.sh`, `test_check_spdx.sh` and `test_venv_hashes.sh`. Run by hand from the repo root:
  - `tests/test_make_fragments.sh` — by hand (`sh tools/tests/test_make_fragments.sh`; runs the real `make test`).
  - `tests/test_check_spdx_edges.sh` — by hand (`sh tools/tests/test_check_spdx_edges.sh`).
  - `tests/test_secret_scan_dir_arg.sh` — by hand (`sh tools/tests/test_secret_scan_dir_arg.sh`; skips without the
    cached gitleaks archive `make secret-scan` downloads).

## Invariants

- Every tool runs from the repository root, takes host values only from arguments or environment, and holds no
  hostname, home path or profile name of a real deployment.
- Checks fail closed: a missing input or a tool error exits non-zero, never 0.
- Tools never touch a live Hermes install; the test Hermes lives under `.local/` (gitignored).
- Every tool directory is listed in `tools/quality/modules.txt` (README gate).
