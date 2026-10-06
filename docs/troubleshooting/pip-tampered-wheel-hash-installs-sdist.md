<!-- SPDX-License-Identifier: Apache-2.0 -->
# A tampered wheel hash still installs: pip falls back to the sdist

## Symptom
A hash-pinning check edits one `--hash=sha256:` value in `requirements-dev.txt` and expects `make venv` to fail, but
`pip install --require-hashes` exits 0. The log shows `Downloading <name>-<version>.tar.gz`,
`Building wheel for <name>` and `Successfully installed`.

## Context
Proving that `--require-hashes` refuses tampered input. Most pins list the hash of every wheel **and** the sdist.

## Cause
pip filters candidate files by the allowed hashes before downloading. When the wheel's hash no longer matches, pip
picks the sdist (its hash still matches) and builds it. The build step installs build backends (setuptools …) into
an isolated build environment **without** hash checking. Nothing tampered gets installed, but the test proves nothing
and the build dependencies are unpinned.

## Fix
To prove refusal, change **every** hash of one package (or the hash of the only file pip can use). Then pip prints
`THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE`, exits 1 and installs nothing. To rule out
sdist builds entirely, install with `--only-binary :all:`: `make venv` does this, so a tampered wheel hash is now
refused with the same error (rc 1, nothing installed). `tools/tests/test_venv_hashes.sh` (offline, part of
`make test`) checks it.

## Verified
Both hashes of `sortedcontainers==2.4.0` changed → rc 1, `pip freeze` empty. Only the wheel hash changed → rc 0,
sdist built. After adding `--only-binary :all:` to `make venv`: the same wheel-only tamper → rc 2 (`make`), `DO NOT
MATCH THE HASHES`, no `Building wheel`, nothing installed.

## Related
[`pip-require-hashes-missing-hash.md`](pip-require-hashes-missing-hash.md),
[`pip-find-links-no-hash-prefilter.md`](pip-find-links-no-hash-prefilter.md)
