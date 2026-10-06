<!-- SPDX-License-Identifier: Apache-2.0 -->
# Gateway exits with code 78 at start-up

## Symptom
`hermes gateway run` stops right after start with exit status **78**; the log mentions a
"non-retryable startup conflict".

## Context
A platform plugin (e.g. `glasses`) is the only enabled platform and its adapter's `connect()` fails.

## Cause
When the only platform reports a **non-retryable** fatal error, the gateway's start-up code treats it as a
configuration conflict and exits 78 (EX_CONFIG) instead of retrying.

## Fix
Read the error the plugin logged just before the exit and fix the setting it names, then start again. Plugin
authors: report transient failures as retryable so the gateway keeps running and reconnects.

## Verified
Verified on an isolated test gateway, Hermes `a4648c5f58`, 2026-10-04: a stub adapter with a non-retryable error →
exit 78; retryable → gateway stays up and retries.

## Related
`glasses-platform-refused-allow-all.md`
