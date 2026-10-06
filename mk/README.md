<!-- SPDX-License-Identifier: Apache-2.0 -->
# Make fragments

## Purpose

`make test` (top-level `Makefile`) runs the secret scan plus every target a fragment here appends to `TEST_TARGETS`.
Each component adds its **own** `mk/<name>.mk` instead of editing the `Makefile`, so branches never collide on it.

## Public API

The `Makefile` includes `$(sort $(wildcard mk/*.mk))` (alphabetical order) after setting:

| Variable | Default | Meaning |
|---|---|---|
| `PY` | `.local/venv-dev/bin/python` | Python of the test venv (`make venv` builds it from `requirements-dev.txt`) |
| `TEST_TARGETS` | empty | Targets run by `make test`; fragments append with `+=` |

Template of a fragment:

```make
# SPDX-License-Identifier: Apache-2.0
PY ?= .local/venv-dev/bin/python

.PHONY: test-protocol
TEST_TARGETS += test-protocol

test-protocol:
	$(PY) -m pytest protocol/tests -q
```

## Invariants

- First line `# SPDX-License-Identifier: Apache-2.0`; targets are `.PHONY`; those `make test` runs are appended with
  `TEST_TARGETS += <target>`.
- Target names carry the component (`test-protocol`, `check-size`, `lint-py`, `coverage-py`, …) and never redefine
  `test`, `help`, `secret-scan`, `venv` or another fragment's targets/variables.
- Recipes run from the repo root, name tracked files by relative path, run the test venv as `$(PY)` and hold no
  host-specific values (hostnames, home paths, profile names). A fragment also runs alone: `make -f mk/<name>.mk PY=.local/venv-dev/bin/python <target>`.
- Wiring tests: `tools/tests/test_make_fragments.sh` (by hand — it runs the real `make test`, so it stays out of
  `TEST_TARGETS`) and `tools/tests/test_make_fragments_edges.sh` (throwaway copies; in `make test` via `mk/skeleton.mk`).
