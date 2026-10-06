<!-- SPDX-License-Identifier: Apache-2.0 -->
# Glasses platform refused: allow-all or wildcard users

## Symptom
The glasses platform does not start or drops connections; the log contains one of
`GLASSES_ALLOW_ALL_USERS is set; it must never be set (list device ids in GLASSES_ALLOWED_USERS instead)`,
`GLASSES_ALLOWED_USERS contains a wildcard '*'; list device ids explicitly`,
`GATEWAY_ALLOW_ALL_USERS is enabled and GLASSES_ALLOWED_USERS is empty: the gateway would admit every glasses sender`
— possibly prefixed with `profile <name>:`.

## Context
Hermes gateway with several profiles; the variables may sit in any profile's `.env`, not only the process
environment.

## Cause
The plugin refuses every setting that would let the gateway admit any sender on the glasses platform, in **every**
profile scope (fail closed). This is intended.

## Fix
Remove the named variable from the named profile's `.env` (or the process environment) and list the device ids in
`GLASSES_ALLOWED_USERS`. Restart the gateway.

## Verified
Plugin tests and test-gateway runs, 2026-10-04: each variant in each scope is refused with the message above.

## Related
`hermes-gateway-exit-78-plugin-startup.md`
