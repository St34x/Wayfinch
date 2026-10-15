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
The device-CA signer and the root-side installer are planned: they are built and reviewed outside this repository
(by the operator's admin agent, milestone M3) and are not part of a deployment today. Once installed, the signer
re-signs its CRLs daily (30-day `nextUpdate`) and publishes them in `signer.public_dir`, and the installer copies
them onto the edge and reloads it. When every device is rejected, check that the signer and the installer you
installed run (their own status and logs) and that the edge holds the current CRL bundle. To have the signer
generate and publish fresh CRLs now, run `python -m glasses.admin crl` the same way as the check below (it uses
`signer.socket` and `signer.user`; exit 1 with one error line when the signer is down, too slow or refuses).

The plugin cannot read the edge copy. To see the published CRLs before they expire, run
`python -m glasses.admin crl --check --hermes-home <Hermes home>` daily from your own scheduler (cron or a systemd
timer), as the plugin user, with the Python that runs Hermes, from the directory that holds the plugin's `glasses/`
package, and alert on any non-zero exit. `<Hermes home>` is the directory that holds `config.yaml`; the check reads
`signer.public_dir` and the state dir (`state_dir`, else `<Hermes home>/glasses`) from there. Without `--hermes-home` the job uses
`$HERMES_HOME`, else `~/.hermes`. `--config <file>` only picks the YAML file, not the state dir: add it only when the
config is not `<Hermes home>/config.yaml`.

- `0` - clean;
- `3` - warnings only: a CRL expires within 14 days or a CA within 180 days (summary `crl check: WARN`);
- `1` - errors: `device-ca.pem` or `devices-crl.pem` is missing, damaged, a symlink or not a regular file, or holds
  anything but its PEM blocks (a private key block in `device-ca.pem` is an error); a CRL is not signed by exactly
  one CA of the bundle or a CA has no CRL; a CRL is rolled back (lower number than the plugin's `crl-seen.json`
  records), dated in the future, has a CRL Number outside 0..2^159, or has expired; a CA is not yet valid, has
  expired, its self-signature does not verify or its public key cannot be loaded; `crl-seen.json` is damaged or
  unsafe (every other check still runs and the record is left as it is); or the glasses config has errors. Warnings
  are printed with the errors.
- `2` - the config file is unreadable, `signer.public_dir` is not set, or a Python module is missing.

Lines name a CA by its `ca_id` (16 hex of the SHA-256 of its public key, as the signer names it). The check reads
only the signer's published files, never the copy on the edge. It is a deployer's early warning, not the
installer's validation: `OK` does not mean the installer accepts the files (an unknown critical CRL extension, an
equal CRL Number with other bytes, AKI = SKI and CRL v2 are not checked here).

## Verified
Verified on nginx with a test device CA, 2026-10-04: expired CRL → good cert rejected; fresh CRL + reload → accepted.

## Related
—
