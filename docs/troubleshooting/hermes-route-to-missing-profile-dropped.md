<!-- SPDX-License-Identifier: Apache-2.0 -->
# Message on a glasses lane gets no answer

## Symptom
A message sent on a lane is acknowledged but never answered; only a WARNING about the route appears in the gateway
log.

## Context
Multiplexed gateway with `gateway.profile_routes` mapping glasses lanes to profiles.

## Cause
A route that points to a profile that does not exist (or is not served) is dropped by the gateway without telling
the client.

## Fix
Check `gateway.profile_routes` against `hermes profile list`; fix the profile name or remove the lane from
`glasses.lanes`. The plugin rejects lanes whose profile is not served (error frame) once that check is in place.

## Verified
Verified on an isolated multiplexed test gateway, Hermes `a4648c5f58`, 2026-10-04.

## Related
`hermes-plugin-platform-has-no-tools.md`
