<!-- SPDX-License-Identifier: Apache-2.0 -->
# `make venv` fails: hashes are required

## Symptom
`ERROR: Hashes are required in --require-hashes mode, but they are missing from some requirements.` or
`THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.`

## Context
Adding or bumping a dev dependency in `requirements-dev.txt` (installed with `--require-hashes`).

## Cause
Every package — including transitive ones — needs `==` pins and the sha256 of every file pip may pick.

## Fix
Install the new set in a scratch venv, take its `pip freeze`, and add for each pin **all** sha256 values from
`https://pypi.org/pypi/<name>/<version>/json` (every wheel and the sdist). Verify with `make venv VENV=<scratch dir>`
and compare `pip freeze` with the file.

## Verified
Used for the hash-pinned `requirements-dev.txt`, 2026-10-04.

## Related
—
