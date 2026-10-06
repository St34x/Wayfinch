<!-- SPDX-License-Identifier: Apache-2.0 -->
# Warning `OSError: AF_UNIX path too long` from the gateway watchdog

## Symptom
Gateway log: `gateway.shutdown_watchdog` … `OSError: AF_UNIX path too long`.

## Context
`HERMES_HOME` is a deep path (more than ~70 characters).

## Cause
The watchdog's liveness socket `<home>/state/gateway.loop-tick.<pid>.sock` exceeds the 108-byte Unix socket path
limit.

## Fix
Harmless: only the liveness tick is disabled. To silence it use a shorter `HERMES_HOME`.

## Verified
Verified on isolated test gateways with a deep `HERMES_HOME`, 2026-10-04: seen on every run; no functional effect.

## Related
—
