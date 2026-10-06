<!-- SPDX-License-Identifier: Apache-2.0 -->
# Every device rejected: `CRL has expired`

## Symptom
All phones fail to connect at once (TLS handshake error on the phone); nginx `error.log`:
`client SSL certificate verify error: (12:CRL has expired)`, access log `verify="FAILED:CRL has expired"`, status 444.

## Context
The TLS edge checks device client certificates against the device CRL (`ssl_crl`).

## Cause
OpenSSL rejects **every** certificate of a CA whose CRL is past its `nextUpdate`, not only revoked ones.

## Fix
Regenerate the CRL (`glasses-admin crl`, 365-day `nextUpdate`), install it into the edge's TLS directory and reload
the edge. Keep the daily expiry check enabled (warns 30 days ahead).

## Verified
Verified on nginx with a test device CA, 2026-10-04: expired CRL → good cert rejected; fresh CRL + reload → accepted.

## Related
—
