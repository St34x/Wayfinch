<!-- SPDX-License-Identifier: Apache-2.0 -->
# Offline hash test: tampered wheel is downloaded instead of the sdist fallback

## Symptom
An offline test of `pip install --require-hashes` with `PIP_NO_INDEX=1 PIP_FIND_LINKS=<dir>` tampers only the wheel
hash and expects pip to fall back to the sdist (as it does against PyPI), but pip picks the wheel anyway and fails:
`Processing <dir>/<name>-<version>-py3-none-any.whl` … `THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS
FILE.` A control run without `--only-binary :all:` therefore proves nothing.

## Context
Reproducing online pip behaviour with local fixture files (no network), e.g. `tools/tests/test_venv_hashes.sh`.

## Cause
pip pre-filters candidate files by the pinned hashes only when the link itself carries a hash. Index pages (PyPI)
give `#sha256=` on every link; plain files in a `--find-links` directory have no hash, so pip picks the best file
first and checks the hash after the download.

## Fix
Serve the fixtures as a simple index with hash fragments: `simple/<name>/index.html` containing
`<a href="file:///…/<file>#sha256=<hex>"><file></a>` per file, and set `PIP_INDEX_URL=file://…/simple/` (no
`PIP_NO_INDEX`). pip then behaves as it does against PyPI.

## Verified
`tools/tests/test_venv_hashes.sh`: with find-links the control (no `--only-binary`) failed with `DO NOT MATCH`; with
the `file://` index it built the sdist (`Building wheel for fxpkg`, rc 0), as it does against PyPI.

## Related
[`pip-tampered-wheel-hash-installs-sdist.md`](pip-tampered-wheel-hash-installs-sdist.md)
