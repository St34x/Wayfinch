<!-- SPDX-License-Identifier: Apache-2.0 -->
# Secret scan fails with "tool error, not a leak"

## Symptom
`make test` stops with `gitleaks failed on the working tree (exit 1) - this is a tool error, not a leak; scan
incomplete` (often after `FTL unable to load gitleaks config`).

## Context
`tools/secret-scan.sh` runs pinned gitleaks with `--exit-code 3`: 3 = leaks found, anything else = gitleaks itself
failed.

## Cause
A broken `.gitleaks.toml`, an unreadable file, or a `noexec` temporary directory — not a secret.

## Fix
Fix the reported cause (`gitleaks` prints it above: config syntax, permissions); real findings are reported as
"leaks found" (values redacted). Never "fix" a tool error by deleting files.

## Verified
Secret-scan tests, 2026-10-04: broken config → rc 2 with this message; planted key → rc 1 "leaks found".

## Related
—
