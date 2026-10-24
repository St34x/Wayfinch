<!-- SPDX-License-Identifier: Apache-2.0 -->
# Admin CLI (`glasses-admin`)

The operator CLI of the `glasses` plugin. This page lists what the code does **today**; the design around it is in
[architecture.md](architecture.md) (§1 and §4). The CLI reference is hand-written until the planned generated one
lands ([ENGINEERING.md §10](../ENGINEERING.md#10-gates)); `--help` of every command is authoritative.

Run it with the Python of the Hermes venv and the plugin directory on the module path:
`python -m glasses.admin <command> ...` (the program name in `--help` is `glasses-admin`). Every command that reads a
config takes `--config FILE` (default `$HERMES_HOME/config.yaml`) and `--hermes-home DIR` (default `$HERMES_HOME`, else
`~/.hermes`). The signer commands (`crl`, `revoke`) print fixed texts: no traceback and no answer text of the signer or the plugin.
A value that contains `://` in `--config`, `--hermes-home`, `$HERMES_HOME` or `$HOME` is refused with exit 2 and one line
(`error: $HOME: not a local path ...`) that names the source, never the value; `$HOME` is tested only when it supplies
the default home, that is when neither `--hermes-home` nor a non-empty `$HERMES_HOME` does.

| Command | What it does | Exit status |
|---|---|---|
| `config check [--live]` | Validates `gateway.platforms.glasses` the way the gateway merges and validates it, plus the allow-all gate; one line per problem and a summary. `--live` also checks the host and the routed Hermes profiles (state dir, `bridge_port` free on 127.0.0.1, `public_url` DNS, lane routes, STT, tool set, clarify and approval timeouts); it imports Hermes under a throwaway home and writes nothing into the Hermes home | 0 no errors (warnings allowed), 1 errors, 2 cannot check (unreadable file, missing module, `--live` on a home the effective user does not own, a `://` in `--config`, `--hermes-home`, `$HERMES_HOME` or the `$HOME` that supplies the default home) |
| `crl` | Asks the device-CA signer (`signer.socket`, `signer.user`) to generate and publish the device CRL; prints one line per CA. The plugin holds no CA key and signs nothing | 0 published, 1 signer call failed or refused (or the config has errors), 2 unreadable config, a `://` in `--config`, `--hermes-home`, `$HERMES_HOME` or the `$HOME` that supplies the default home, a `signer.*` key not set, a module missing |
| `crl --check` | Checks the CA and CRL files the signer publishes in `signer.public_dir` (needs `cryptography`): CRL expiry and rollback (record `crl-seen.json` in the state dir), CA validity. Warns 14 days before a CRL and 180 days before a CA expires. Never reads the proxy's copy | 0 clean, 1 errors, 3 warnings only, 2 as for `crl` |
| `revoke DEVICE_ID` | `DEVICE_ID` is 16 lower-case hex. Asks the signer's `revoke` op to write the tombstone and the CRL, then asks the running plugin over `$state_dir/admin.sock` for the live cut. A signer that does not know the device, or fails, still gets the plugin leg asked, and the exit is then 1. A config with errors stops it before both steps. Writes no file | 0 signer and plugin confirmed, 1 a leg not confirmed or the config has errors, 2 bad device id, unreadable config, a `://` in `--config`, `--hermes-home`, `$HERMES_HOME` or the `$HOME` that supplies the default home, missing module, usage error |

`--live` and the other options of `config check` are described in the module docstring of `hermes-plugin/glasses/admin.py`;
what `--live` may print of a `.env` or of the environment, and its known limits, are in
[test-hermes.md](test-hermes.md). The settings the commands read are in the
[configuration reference](reference/config.md) (`signer.*`, `state_dir`).

## Not built yet

- The server half of `$state_dir/admin.sock` and the live cut (close code `4403` within one second, outbox purge, ...):
  planned, T1.63a. Until then `revoke` can only confirm the signer leg and says so. Once the signer has written the
  tombstone, the running plugin closes an open session of that device with `4403` within about a second (its stat poll
  of the signer's `devices.json`, T1.248: the file-change backstop for the signer's revocations), so no gateway restart
  is needed for them; the rest of the live cut (token kill, deny overlay, alert) is planned.
- `list` (published device registry): planned, T1.15f.
- Opening an enrolment window, the device CA and certificate issuance: these belong to the separate signer service,
  which is built outside this repository (planned). Windows are opened from the signer's own root side, never by
  `glasses-admin`, the plugin or an agent. There is no `ca` or `enroll` command in this CLI.
- The plugin's daily `crl --check` run: planned; schedule it yourself (see
  [CRL has expired](troubleshooting/nginx-crl-expired-rejects-all-devices.md)).
