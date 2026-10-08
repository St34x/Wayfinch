<!-- SPDX-License-Identifier: Apache-2.0 -->
# pytest `--basetemp` fails: FileNotFoundError on the basetemp dir

## Symptom
Every test that uses `tmp_path` errors at setup, the others pass:
`FileNotFoundError: [Errno 2] No such file or directory: '<repo>/.local/pytest-tmp/<dir>'` and
`ERROR test_x.py::test_x - FileNotFoundError: [Errno 2] No such file or direct...`, pytest exit code 1.

## Context
A pytest call with `--basetemp=.local/pytest-tmp/<dir>` (as in every `mk/*.mk` recipe) in a fresh clone or a
scratch tree where `.local/pytest-tmp/` does not exist yet.

## Cause
pytest creates the basetemp dir with a plain `mkdir` without parents (and empties it first if it exists), so the
parent dir must already exist. The error appears lazily, at the first `tmp_path` request, not at start-up.

## Fix
Create the parent in the same recipe line before the call:
`mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/<dir> …`. Give every call its own
`<dir>`: pytest empties the basetemp per run, so two calls sharing one would delete each other's files.

## Verified
pytest 9.1.1, a one-test file using `tmp_path`, in an empty dir: without the parent → `1 error`, rc 1; after
`mkdir -p .local/pytest-tmp` → `1 passed`, rc 0.

## Related
`mk/README.md` (Invariants), `ENGINEERING.md` section 6 (temp dirs),
`tools/quality/tests/test_check_hygiene_pytest_basetemp.py`.
