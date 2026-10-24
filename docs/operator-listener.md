<!-- SPDX-License-Identifier: Apache-2.0 -->
# Operator guide: the edge listener

What an operator sets up and reads for the plugin's listener. The keys are in the
[configuration reference](reference/config.md); the design is in [architecture.md](architecture.md). Values below are
examples (`example.com`, `/run/hermes-glasses/edge.sock`, the user `edge-proxy`).

## The socket and who may use it

- The plugin serves `/ws` and `/enroll` only on the unix socket `edge.socket` (mode 0660), never on TCP.
- Only `/ws` consults the allow-all gate when a request arrives (`503` while it is open). `/enroll` does not: the
  signer's enrolment window is the gate there. The adapter checks the gate once, when it starts the listener, and
  starts it for both paths only while the gate is closed. A gate that opens later does not stop the listener: `/ws`
  then answers `503` and `/enroll` is still served.
- Only a process running as `edge.user` (the system user of the TLS edge, e.g. `edge-proxy`; never root) is admitted.
  The plugin checks the peer's uid with `SO_PEERCRED` when a connection is accepted and again for each request; any
  other peer is closed before a byte is read.
- Mode 0660 gives the socket's owner (the gateway user) and its group read and write; the plugin does not change
  owner, group or ACL, so the edge user's access is an operator set-up step (as root), either (a) a group: make the
  edge user a member of the group the socket gets, e.g. a `tmpfiles.d` entry `d /run/hermes-glasses 2750 <gateway user>
  <shared group> -` (setgid, so a new socket takes the directory's group), with the edge user in `<shared group>`, or
  (b) an ACL on the directory: `setfacl -m u:edge-proxy:x <dir>` and `setfacl -d -m u:edge-proxy:rw <dir>`. Without
  one of them the edge user cannot connect; `edge.user` only decides who is admitted after the connection is made.
- The directory of the socket must exist and be writable by the gateway service. When it is not, the plugin does not
  start the listener and the error names the systemd drop-in line `ReadWritePaths=-/run/hermes-glasses`. That hint is
  fixed text: when `edge.socket` is in another directory, write that directory in the drop-in instead.
- At start the plugin replaces a stale socket of its own user (nothing listens on it) and refuses anything else. The
  gateway log then has a WARNING line `glasses adapter: edge listener not started: ` followed by one of these texts, and
  the plugin reports "not connected" and retries with backoff:
  - `edge.socket: the path exists and is not a stale socket of this user; left untouched`: the path is a regular file,
    a directory, a link or a socket of another user. Remove or move it by hand, or point `edge.socket` elsewhere.
  - `edge.socket: in use by another process; left untouched`: a process listens on the path (the test connection
    succeeded). Another gateway (or a second profile) is listening there. Stop it or give each instance its own
    `edge.socket`; the plugin never unlinks a live socket.
  - `edge.socket: cannot tell whether another process uses it; left untouched`: the test connection to the existing
    socket failed in a way that is neither "nobody listens" nor "connected" (for example a listener that does not
    answer because its backlog is full). Look for the process that holds the socket (`ss -xlp`); when none does,
    remove the file by hand and the next retry starts.
- The edge terminates TLS for `https://example.com/ws`, requires a client certificate there, and proxies to this
  socket with the client certificate in the header `client_cert_header` (default `X-Client-Cert`), encoded per
  `client_cert_encoding`: `pem-urlescaped` is nginx's `$ssl_client_escaped_cert`, `der-base64` Caddy's base64 DER.
- What the plugin trusts in that header: nothing it has not checked. It verifies the certificate against the signer's
  `device-ca.pem` (signed by a CA of the bundle whose key id matches the certificate's authority key id, valid now,
  `CA:false`, key usage digitalSignature, extended key usage clientAuth, no critical extension other than those, an
  EC key on P-256, P-384 or P-521 and a SHA-256/384/512 signature; exactly one certificate in the header) and takes
  the device identity from the SHA-256 of that certificate, which must match the registry together with the token. A
  header that names a fingerprint, such as `X-Client-Cert-Fp`, is never read. Any failure is the same `401`.
- What it cannot check: that the sender holds the certificate's private key. A certificate is not a secret, so proof
  of possession is the TLS handshake at the edge. Hence two requirements remain on the edge: (a) it sets the header
  from the client certificate of the TLS connection and replaces any copy the client sent (never passes a client
  value through), and (b) `edge.user` runs only the edge: any other process of that user can send any device's
  certificate, and with that device's token it is admitted. Certificate revocation lists are the edge's: nginx
  applies its `ssl_crl` with `ssl_verify_client on`, `optional` and `optional_no_ca` alike, with the device CA as the
  client trust anchor (`ssl_client_certificate`): OpenSSL still checks the chain and the CRL, and `optional_no_ca`
  only makes nginx ignore an unknown issuer, so a revoked device certificate is refused before any location runs.
  What the client then receives depends on the edge: `495` is nginx's internal code for a failed client-certificate
  check. Without an `error_page` for it the client gets `400` ("The SSL certificate error"); with an `error_page`
  mapping for it (for example to `444`) the client gets that instead (`444` closes the connection with no answer).
  `optional` stays the recommended setting: it also refuses a certificate of an unknown CA before any
  location runs, where `optional_no_ca` lets it through to the `if ($ssl_client_verify != SUCCESS)` check of the `/ws`
  location. Without a CRL at the edge, a revoked device whose certificate is still valid reaches the plugin, costs a
  gate call and a registry read per attempt and is refused there by its registry tombstone (`403`). The plugin's own
  revocation is the registry's tombstone and its deny overlay; it reads no CRL.

## The access log

The plugin writes one INFO line per request on the logger `aiohttp.access`:

```text
remote "METHOD path" status ms
```

- Over a unix socket the remote is empty, so every line starts with a space: ` "GET /ws" 401 3`.
- The path has no query string (a credential a client puts in the URL stays out of the log); `ms` is an integer.
- A request the HTTP parser refuses (garbage bytes, a bad method or version, an oversized or malformed header) is
  answered `400` with the status phrase only and writes one INFO access line, ` "UNKNOWN /" 400 0`, and one DEBUG line
  on `aiohttp.server` with the exception type only (`glasses ws: request refused by the HTTP parser (<type>)`).
  Neither carries any text of the request. The tail a client pipelines behind a refused upgrade writes only the DEBUG
  line (type only) and no access line, and it gets no answer: the upgrade's `401` is lost too, with no access line, and
  the connection ends. The plugin does not bound how many such lines a peer can write; log volume control belongs to
  the edge (next item).
- This log cannot attribute a flood to a client, and nothing in the plugin tries to. Attribution and per-client flood
  control belong to the edge: its own access log (client address, certificate fingerprint) and a `limit_req` zone. We
  recommend a zone keyed on `$ssl_client_fingerprint` with `limit_req_status 429`, for example 30 r/m with burst 10
  for `/ws`; the plugin does not install or check it.
- The plugin's cap of 8 `/ws` upgrades in flight counts per verified client certificate: one certificate at the cap
  does not hold back another. Over it the answer is `503` with `Retry-After: 5`, as it is for a gate or a registry
  that does not answer (see the `503` row in the [protocol](../protocol/README.md)). The registry reads of all
  clients together are bounded at 32 requests.

## The signer's public directory

`signer.public_dir` holds `devices.json` and the CA bundle `device-ca.pem` (and the CRL file), written by the signer
service only.

- The directory and every parent are owned by root or the signer user and are not writable by the plugin user. The
  plugin checks the directory itself, not its parents: it refuses a directory that is a symlink, owned by the plugin
  user, or writable by group or others.
- `devices.json` and `device-ca.pem` are owned by the signer user, mode 0640 or stricter. The plugin refuses either
  when it is missing, a symlink, owned by anyone else, wider than 0640, oversize or corrupt; `device-ca.pem` must hold
  1 to 4 CA certificates (`CA:true`, key usage keyCertSign, an EC key on P-256, P-384 or P-521 and a
  SHA-256/384/512 signature, no other critical extension) and nothing else. The plugin checks that signature's hash
  algorithm but does not verify a CA certificate's self-signature; it relies on the file's owner and mode, since
  only the signer user can write it. A device certificate's signature is verified, with the key of the CA that issued
  it.
- A refusal means no new device connection is accepted (the upgrade answers `401`). The plugin looks at
  `device-ca.pem` again at most once a second and re-reads it when it changed, so a CA added or removed by a rotation
  counts without a restart. Cutting sessions that are already live when a file turns bad is the watcher of T1.63a,
  not built yet.

## Open edge items

These are for the edge's administrator to decide; the plugin does not decide them.

- Header read timeout: aiohttp bounds nothing before a request is complete, so a client that sends its request head
  slowly (or not at all) holds a connection of the edge user. Whether the edge sets a read timeout toward the socket
  is open.
