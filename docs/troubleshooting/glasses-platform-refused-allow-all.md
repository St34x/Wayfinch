<!-- SPDX-License-Identifier: Apache-2.0 -->
# Glasses platform refused: allow-all or wildcard users

## Symptom
The glasses platform does not start or drops connections; the log contains one of
`GLASSES_ALLOW_ALL_USERS is set; it must never be set (list device ids in GLASSES_ALLOWED_USERS instead)`,
`GLASSES_ALLOWED_USERS contains a wildcard '*'; list device ids explicitly`,
`GATEWAY_ALLOW_ALL_USERS is enabled and GLASSES_ALLOWED_USERS is empty: the gateway would admit every glasses sender`
— possibly prefixed with `profile <name>:` and followed by `; or remove the switch from that profile`. Or the
fixed line `allow-all gate check timed out; refusing` (see Cause).

## Context
Hermes gateway with several profiles; the variables may sit in the `.env` of any profile the gateway serves, not
only the process environment. A parked or standalone profile is not read.

## Cause
The plugin refuses every setting that would let the gateway admit any sender on the glasses platform, in the
process environment and in **every served** profile scope (fail closed). This is intended. The verdict is reused for
one second and then always recomputed, whatever the source of the switch (a profile's `.env`, the
administrator-managed `.env`, an external secret, `config.yaml` `secrets:`), so a switch added or removed takes
effect within a second (a change of the process environment, or a different active scope in the context of the
caller, at once).

A refusal is logged once per change, not per attempt: one WARNING line per problem, `glasses adapter: refusing: ...`,
when the set of problems differs from the one logged last (the first refusal, or a changed set), one INFO `allow-all gate closed
again; accepting` when the gate is clean again, and one WARNING `inbound event dropped (allow-all gate open)` for
the first dropped event of a refusal (DEBUG for the rest). While the refusal stays the same the log stays quiet.
The log therefore shows the start of a refusal, not its length: the line is written when the refusal changes (once
per adapter, not once per caller), so how long it has lasted is not in the log.

If a read of a scope blocks (a `.env` that is a FIFO, a hung network home), the check that runs it holds the
verdict's lock. Every other check waits at most 2 seconds and is then refused with `allow-all gate check timed
out; refusing` (no value in the text); once the read has blocked for more than 2 seconds, new checks are refused at
once. The check that runs the blocked read itself also gives up, after about 3 seconds (2 seconds plus 1), and is
refused (`allow-all gate check failed (TimeoutError)`). The checks run in the adapter's own two worker threads, so
they never delay other work in the gateway's event loop or its default thread pool. Nothing is admitted meanwhile,
and the next check after the blocked read ends is judged normally. Fix the blocked path (a regular file, a
reachable home).

## Fix
Remove the named variable from the named profile's `.env` (or the process environment) and list the device ids in
`GLASSES_ALLOWED_USERS`. If the profile's `.env` does not hold it, the value comes from another source of that
profile's scope: the administrator-managed `.env` (`$HERMES_MANAGED_DIR`, default `/etc/hermes`), an external
secret or the `secrets:` section of its `config.yaml`. The adapter sees the change within a second; the gateway's reconnect
watcher retries the platform (backoff up to 300 s). A gateway stopped while a scope read still blocks can wait
until the service manager's stop timeout ends it: the blocked worker thread cannot be cancelled and is joined when
the process exits.

## Verified
Plugin tests and test-gateway runs, 2026-10-04: each variant in each scope is refused with the message above.
Scope (served profiles only) and cache: `hermes-plugin/tests/test_gate.py`. Lock timeout and log lines:
`test_gate_cache_lock.py`, `test_gate_refusal_log.py`.

## Related
`hermes-gateway-exit-78-plugin-startup.md`
