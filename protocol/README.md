<!-- SPDX-License-Identifier: Apache-2.0 -->
# Wayfinch protocol — envelope v1

## Purpose

`envelope.schema.json` (JSON Schema draft 2020-12) is the contract between the phone app (**device**, D) and the
server plugin (**server**, S). Every WebSocket text frame is one JSON object (an *envelope*). Both stacks validate
frames against this schema; golden fixtures live in `fixtures/` (T1.03).

## Public API

- `envelope.schema.json` — the contract (JSON Schema draft 2020-12): the envelope, the `type` enum and one
  `$defs/body.<type>` per frame type.
- `fixtures/` — golden frames (`valid/`, `invalid/`, `codec-valid/`, `codec-invalid/`, `raw-invalid/`: text that is
  not JSON or repeats a key) and `lexical.json` (bare values, control characters, escapes, whitespace) both stacks
  test against.
- This document: the field, message-type, close-code and error-code tables below, the delivery rule, the HTTP
  `/enroll` request and answers (the one call outside the WebSocket), and the
  limits table (UTF-8 byte limits per field) in *Limits and validation notes*. Tests and `tools/codegen/` parse the
  message-type and limits tables, so their row format is part of the API.

## Invariants

- One WebSocket text frame = one envelope object; no top-level field outside the envelope table, and every body
  schema has `additionalProperties: false` and a bound on every field — except the open objects
  `skill.call.args`, `skill.result.output` and `skill.manifest.skills[]` items, bounded only by `maxProperties`
  (see *Limits and validation notes*).
- String limits are byte limits, which the codecs must enforce: the server-side codec
  (`hermes-plugin/glasses/envelope.py`, `envelope_encode.py`) enforces them on every frame it decodes or encodes
  (the WS session that uses it is built, `hermes-plugin/glasses/server_session.py`, T1.07b, and served on the edge's unix socket; the phone's `decodeEnvelope` enforces only part of them, see *Kotlin decoder* below); the `codec-valid/` and
  `codec-invalid/` fixtures fix the behaviour. The schema's `maxLength` is the same number as a code-point bound,
  so the schema never rejects what the byte rule accepts.
- Schema, these tables and the fixtures change together; the generated Python and Kotlin types are regenerated
  (`make check-codegen`; the Kotlin ones are also tested against the fixtures on a machine with a JDK and the Android SDK, `make test-android-jvm`)
  and `make test-protocol` stays green.
- Delivery rule: S→D-only frames carry `seq` and stay outboxed until acked (`welcome` excepted: no `seq`, never
  outboxed); D→S frames never carry `seq` and are
  processed at most once (Delivery). The server-side codec enforces the `seq` rules (the WS session
  `server_session.py` uses it: T1.07b, served on the edge's unix socket); the outbox is built (T1.09b: `outbox.sqlite`, `seq` per device, ack, replay after `lastAckedSeq`, TTL 24 h, at most 500 per device, purge of a revoked device; the byte-bounded send queue and the session deadlines are T1.09c, planned); the dedupe by `id` is built for `msg.in` only (T1.08: in memory, per device, 10 min, at most 1024 ids, forgotten on restart).

## Envelope

| field | required | meaning |
|---|---|---|
| `v` | yes | protocol major, `1`. A mismatch ⇒ `error` `unsupported_version` + close `4400`. |
| `id` | yes | ULID, unique per sender. D→S frames are processed at most once (dedupe by `id` within 10 min). |
| `type` | yes | one of the types below; selects the `body` sub-schema. |
| `ts` | yes | RFC 3339 date-time with offset (`Z` or ±hh:mm ≤ ±14:00), e.g. `2026-10-03T18:00:00Z`; field ranges are checked (see Limits). |
| `seq` | S→D | per-device monotonic integer 1…2^53−1, acked by the device. **Required** on S→D-only types except `welcome`; **forbidden** on D→S-only types, on `ping`/`pong` and on `welcome`; **optional** on `error` only (rules in Delivery). |
| `corr` | no | ULID of the frame this one answers. |
| `lane` | no | lane name, pattern `^[a-z][a-z0-9_-]{0,31}$`. A pattern, not a list: which lanes exist is server config (examples here use `assistant` and `work`). |
| `body` | yes | type-specific object; every body schema has `additionalProperties: false` and bounds on every field. |

No other top-level field is allowed. Field names in the design marked `?` are optional; all others are required.

## Message types

`B` = UTF-8 bytes (see Limits).

| type | dir | body (required, `?` = optional) |
|---|---|---|
| `hello` | D→S | `appVersion`, `protocol`, `lastAckedSeq`, `manifestHash`, `glasses{model, fw}` |
| `welcome` | S→D | `deviceId`, `serverTime`, `lanes[]` (≤32), `limits{maxFrame, maxAudioSec}`, `resumeFrom` (the `seq` of the first frame the server replays on this connection; no `seq` on the `welcome` itself) |
| `ping` | both | `{}` (every 25 s) |
| `pong` | both | `{}` |
| `ack` | D→S | `upToSeq` |
| `msg.in` | D→S | `text` (1–2000 B), `origin` `ptt`\|`wake`\|`typed` (`typed` reserved), `langHint?` |
| `audio.start` | D→S | `clipId`, `mime` = `audio/ogg;codecs=opus` |
| `audio.chunk` | D→S | `clipId`, `chunk` (standard base64 with padding, ≤31744 chars = 23808 audio bytes) |
| `audio.end` | D→S | `clipId`, `durationMs` (0–30000) |
| `msg.out` | S→D | `kind` `answer`\|`ack`\|`push`\|`delta`\|`heard`\|`notice`, `text` (≤2048 B; ≤512 B with `longRef`), `chatId` (`glasses:<lane>`), `final`, `notice?{kind, ref?}` (required when `kind` = `notice`, forbidden otherwise), `longRef?{id, channel, profile, status, fullText? ≤4096 B, truncated?}` — `channel` is a platform name (`glasses.long_answers.platform`, default `telegram`), `profile` a Hermes profile name |
| `long.status` | S→D | `id`, `status` `sent`\|`retrying`\|`failed` |
| `approval.request` | S→D | `approvalId`, `summary` (≤300 B), `detail?` (≤4000 B), `choices` ⊆ {`once`,`deny`} (1–2, unique), `expiresAt` |
| `approval.response` | D→S | `approvalId`, `choice` `once`\|`deny`, `via` `glasses`\|`phone` |
| `clarify.request` | S→D | `clarifyId`, `question` (≤2000 B), `choices[]` (0–4, each ≤200 B), `detached` (bool). **No expiry field** |
| `clarify.response` | D→S | `clarifyId` + exactly one of `answer` (≤2000 B) or `dismiss: true` |
| `card.state` | S→D | exactly one of `approvalId` / `clarifyId`, `state` (see below) |
| `hud.state` | S→D | `hermes` `up`\|`degraded`, `pendingApprovals`, `unreadPushes`, `longChannel?` (platform name, or `none` when no long-answer channel is configured) |
| `skill.manifest` | D→S | `manifestVersion`, `skills[]` (≤256 objects; entries validated by `skill-manifest.schema.json`, separate task) |
| `skill.call` | S→D | `callId`, `skill` (`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`), `args{}`, `mode` `read`\|`act`, `deadlineMs` (1–600000) |
| `skill.result` | D→S | `callId`, `status` `ok`\|`denied`\|`expired`\|`invalid`\|`error`\|`unavailable`, `output?{}`, `error?` (≤500 B) |
| `skill.cancel` | S→D | `callId` |
| `meeting.start` | D→S | Phase 4. `meetingId` (ULID), `langHints[]?` (0–8; absent or empty = auto-detect), `hintLevel` `off`\|`low`\|`medium`\|`high`, `prepTerms[]?` (≤50, each ≤100 B), `mic` = `glasses` |
| `meeting.audio` | D→S | Phase 4. `meetingId`, `seq` (per meeting), `mime` = `audio/ogg;codecs=opus` (v1 is Opus-only; PCM s16le 16 kHz only if a later measurement requires it — a schema change), `chunk` (base64, as `audio.chunk`) |
| `meeting.control` | D→S | Phase 4. `meetingId`, `action` `pause`\|`resume`\|`stop`\|`mark`\|`hint_level`, `hintLevel?` (required with `hint_level`) |
| `meeting.state` | S→D | Phase 4. `meetingId`, `state` `starting`\|`active`\|`paused`\|`finalizing`\|`done`\|`error`, `provider`, `elapsedMs`, `error?` (required with state `error`) |
| `meeting.hint` | S→D | Phase 4. `meetingId`, `hintId`, `kind` `TERM`\|`WHO`\|`ANS`\|`TODO`\|`FALSE`\|`TOPIC`, `text` (1–180 B), `ttlMs?` (1000–60000, default 10000) |
| `error` | both | `code` (snake_case ≤64), `message` (≤500 B; never secrets or message bodies) |

`config.change` is **not** a frame type: configuration changes travel as `skill.call` of built-in `config.*` skills.

**Voice path.** `audio.start` / `audio.chunk` / `audio.end` carry a command clip only on the **fallback** path, used
when streaming speech-to-text is not configured on the server or not available; the plugin then hands the clip to the
gateway's configured STT provider. The default (planned) path sends no audio to the server: the phone streams the
command directly to the streaming STT provider with a short-lived token minted by the plugin, and sends only the
final text as `msg.in`. The frames that request and deliver that token are planned additions to this schema.
`meeting.audio` belongs to an earlier meeting design in which the server relayed the audio; in the planned meeting
mode the phone streams to the provider directly and sends transcripts, and that frame is to be replaced.

### Card states

| card | allowed `state` |
|---|---|
| approval (`approvalId`) | `answered`, `expired`, `answered_elsewhere` |
| clarify (`clarifyId`) | `answered`, `answered_elsewhere`, `detached`, `dismissed` |

Approvals expire fail-closed. Clarify cards never expire: `detached` means Hermes moved on without an answer (the card
stays answerable; the answer becomes a new `msg.in`), `dismissed` means the operator dismissed it
(`clarify.response` with `dismiss: true`). The schema rejects `expired` for a clarify card and `detached`/`dismissed`
for an approval card.

## Delivery

One rule, used by the field table above and by both codecs:

- **S→D-only types** (except `welcome`) always carry `seq` and stay in the server outbox until acked (`ack.upToSeq`; TTL 24 h, max 500
  per device; overflow drops the oldest unpinned frames, `push` first, and stores `error` `outbox_overflow`; pending
  approval and clarify cards are exempt from TTL and eviction; built, T1.09b).
- **`ping` / `pong`** (keepalive, both directions) never carry `seq` and are never outboxed. The schema rejects `seq`
  on them.
- **`welcome`** (the server's answer to `hello`, S→D only) is outside the numbering: it never carries `seq`, is never
  stored in the outbox and is never replayed; each connection gets a fresh one. The schema rejects `seq` on it, and the
  codec refuses a `welcome` that arrives from the device. `resumeFrom` tells the device where the numbered frames
  resume: the `seq` of the first frame replayed on this connection, or the next `seq` the server will assign when
  nothing is queued, at most 2^53 − 1 (built, T1.09b; the replayed frames follow the `welcome` in `seq` order).
- **`error`** is the only type where `seq` is optional. An S→D `error` that reports a state the device must not miss
  (e.g. `outbox_overflow`) carries `seq` and is outboxed like any S→D frame; a connection-scoped S→D `error` sent just
  before a close (e.g. `unsupported_version`) has no `seq` and is not outboxed. A D→S `error` never has `seq`.
- **D→S frames** never carry `seq`; they are processed at most once (dedupe by `id` within 10 min). Built for `msg.in`
  (T1.08): in memory per device, at most 1024 ids, forgotten on a restart; a repeated `id` is dropped without an
  answer (a durable dedupe with a receipt is T1.168, planned).

The schema cannot see the direction of a frame, so the codec (T1.06 on the server, the Android codec on the phone)
rejects `seq` on any D→S frame, including `error`; the server's `encode` also refuses every D→S-only type (reason
`direction`). On reconnect the server replays every outboxed frame after
`hello.lastAckedSeq`; the device de-duplicates by `seq`.

## WebSocket close codes

| code | name | meaning |
|---|---|---|
| `4400` | `unsupported_version` | envelope `v` / `hello.protocol` not supported (sent after an `error` frame) |
| `4401` | `unauthorized` | device token or client-certificate fingerprint missing/invalid, detected after the WebSocket upgrade |
| `4403` | `revoked` | the device was revoked: sent at once to a connected device (transport aborted if the close handshake takes >500 ms) and to every reconnect attempt that reaches the upgrade |
| `4409` | `replaced` | a newer connection for the same device took over (one connection per device). The client must not reconnect automatically sooner than 60 s and shows "connected elsewhere"; an app instance that itself holds the newer connection just drops the old socket. Built (T1.07c, `hermes-plugin/glasses/connections.py`): the old connection is closed with this code (transport aborted if the close handshake takes >500 ms) and anything queued for it is dropped; frames sent from then on go only to the new connection |
| `1000` | `normal` | standard RFC 6455 normal close |
| `1001` | `going_away` | standard RFC 6455: server shutdown / restart (built, T1.07c: every connection gets it on shutdown, also a device whose upgrade completes after shutdown began) |
| `1008` | `policy_violation` | standard RFC 6455: the connection exceeded its inbound byte budget (128 KiB per second, burst 128 KiB, each frame charged at least 256 bytes, WebSocket protocol pings included); the frame is not decoded or dispatched. Also a frame the connection's send queue refuses (full or ending): the outbox keeps it and replays it after the reconnect (built, T1.09b). T1.09c will also use it for the hello and idle deadlines (planned, not built) |
| `1009` | `message_too_big` | standard RFC 6455: frame over the size limit |
| `1011` | `internal_error` | standard RFC 6455: the server failed while handling a frame of this connection (the dispatch of a frame, or the outbox at `hello`, e.g. a registry read that failed inside it); nothing more is handled on it. Not a revocation: the phone wipes nothing, emits no `Revoked` and reconnects with its normal backoff |
| `1013` | `try_again_later` | standard RFC 6455: the server could not check the device for its `hello` (a changed `devices.json` it cannot read, or the outbox's revocation sweep that the `hello` waited for could not read the registry, T1.248); nothing was welcomed or replayed and the queued frames are kept for the next `hello`. Not a revocation: the phone wipes nothing, emits no `Revoked` and reconnects with its normal backoff |

Authentication that fails **before** the upgrade (the server checks credentials on the HTTP upgrade request) is
answered with an HTTP status and never reaches a close code; the `44xx` codes are for failures after the upgrade. The
check is built (`hermes-plugin/glasses/server_auth.py`, T1.07a) and called by the WebSocket server (`server.py`, session in `server_session.py`, T1.07b; served on the edge's unix socket only). A peer the server's own check refuses gets the unknown-path
`404` for every method (also `POST /ws`); an allowed peer gets `405` for any method but `GET`.
Every body is fixed JSON (`Content-Type: application/json`), never a token, hash, fingerprint or error text, and only the
revoked answer carries `device_revoked`:

| status | body | when |
|---|---|---|
| `503` | `{"error":"unavailable"}` | the server's allow-all gate is open, or its check fails or does not answer within 5 s: checked after the peer, the client certificate and the upgrade cap and before the token and any registry read (the order is peer, certificate, cap, gate, token, registry), so while the gate is open a request without a valid certificate still gets `401`; also the client certificate already has 8 upgrades in flight (T1.07c, T1.184c: refused before the gate and any registry read; the cap counts upgrades in flight per verified client certificate, so one certificate at the cap does not hold back another; how often one certificate reaches the plugin is the edge's: we recommend a `limit_req` zone keyed on `$ssl_client_fingerprint`, e.g. 30 r/m, burst 10 for `/ws`, status 429) or the server's registry reads are saturated (a flood of upgrades) or the injected revocation check raised or answered something other than a boolean (the device is not wiped; also when the server cannot read a changed `devices.json`: an upgrade that meets its re-read waits for it, at most 2 s, and is refused only when that read fails or times out or the file changed again during it, T1.248); every `503` of `/ws` carries `Retry-After: 5` (seconds; the gate bound) |
| `401` | `{"error":"unauthorized"}` | `Authorization: Bearer <token>` missing, repeated, over 512 bytes or of the wrong shape (a token is 1-256 of `A-Za-z0-9_-`: refused before any registry read); no forwarded client certificate, or one that does not verify against the signer's device CA bundle (checked before the upgrade cap and any registry read; a forwarded fingerprint header is never read); wrong token; the certificate of another device; an unreadable or corrupt device registry, or any failure while reading it (fail closed) |
| `403` | `{"error":"device_revoked"}` | the device is revoked (registry entry, tombstoned fingerprint, or revoked while the server runs; an exact `true` answer of the revocation check): the phone wipes on this marker, an unmarked `403` wipes nothing (built on the phone: `RevocationMarker`, T1.24d, marks only media type `application/json` (ASCII, any case) with at most `charset=utf-8` and a body of at most 1 024 bytes, read through a cap, that is one strict JSON object whose `error` is exactly `device_revoked`) |

After the upgrade (built in T1.07b, `hermes-plugin/glasses/server_session.py`): the first frame must be `hello`, answered by
`welcome` (no `seq`; `resumeFrom` is the `seq` of the first frame replayed, else the next `seq`, at most 2^53 − 1, and the queued frames follow it in `seq` order, T1.09b); `ping` is
answered by `pong` (a server-initiated `ping` every 25 s is planned); a rejected frame gets the `error` frame below and the
session stays up; a message of up to 65 536 bytes (the `maxFrame` of `welcome`) is accepted and a larger one closes with `1009`; each connection has an inbound byte budget (128 KiB per second, burst 128 KiB, every frame charged at least 256 bytes, counted on the received bytes before the frame is decoded) and a frame over it closes the connection with `1008`; WebSocket protocol pings (opcode 9) are frames too: the server loop charges each one, then answers it with a pong of the same payload (a pong from the device is ignored), so a flood of pings is closed `1008` like any other flood while a phone's keepalive pings (e.g. OkHttp `pingInterval`) keep working; a frame of a device revoked in the plugin's deny overlay or by the signer, or one whose revocation check
fails, is dropped and never acknowledged, and a revoked device is closed with `4403` (T1.248: the plugin re-reads the signer's `devices.json` as soon as
its stat (mtime, ctime, size, inode) changes, found by a frame's own check or by a stat poll every 0.5 s, off the event loop on a worker of its own and
bounded by 2 s; a frame or upgrade that meets that re-read is held until it ends, not dropped (a second change of the file during that read: the frame is dropped, the upgrade answered `503`); while a changed file cannot be read (retried at most once a
second, a newer version at once) every frame of a device not already known as revoked is dropped and a `hello` is closed `1013`; a device a re-read finds
revoked is closed `4403` at once, an idle session too, within about a second of the signer's write; an idle session of a device revoked only in the
plugin's deny overlay is cut by the push to a connected device when it is revoked, planned, T1.63a; the server-side `close_device(deviceId, code)` it
will call is built, T1.07c). Every frame the
server sends goes through the connection's one send queue and writer task (protocol pongs aside: the frame loop writes
them directly), so closing a connection discards what is queued and the writer task writes nothing after the close frame.

## Error codes used so far

`unsupported_version`, `outbox_overflow`, `lane_forbidden`, `lane_unrouted`, `provider_down` (meeting state).
New codes must match `^[a-z][a-z0-9_]{0,63}$`.

`lane_forbidden` and `lane_unrouted` answer a `msg.in` (built, T1.08, `hermes-plugin/glasses/inbound.py`): its lane
(or the server's `home_lane` when it names none) is not one of the device's lanes or of the server's `lanes` (`lanes:
[]`: the device's lanes only) (`lane_forbidden`), or is routed to a Hermes profile the server does not serve
(`lane_unrouted`). The `error` is
connection-scoped (no `seq`), `corr` is the `msg.in` id, the `message` is fixed per code; the message is not handed
on. An accepted `msg.in` gets no frame back.

The server-side codec (`hermes-plugin/glasses/envelope.py`) refuses a D→S frame with one of the codes below; its
`error_frame` builds the matching `error` frame without `seq` or `corr` (`server_session.py` sends it, T1.07b; no
listener yet). The checks run in the order of the module docstring; the first one that fails decides the code. The
`message` is fixed per code and never echoes input.

| code | rule that yields it |
|---|---|
| `frame_too_large` | payload over its frame cap (see *Frame caps* below) |
| `frame_too_deep` | the raw nesting pre-scan, or an open field (`skill.call.args`, `skill.result.output`, a `skill.manifest.skills[]` item) nested deeper than 32 |
| `invalid_text` | bytes that are not UTF-8; a string or key that is not Unicode scalar values (lone surrogate) |
| `invalid_json` | not one JSON text; `NaN`/`Infinity`; a number literal too large for a finite float (`1e999`); a key repeated in one object |
| `invalid_frame` | fails the envelope schema; Feb 29 of a common year; a non-integer spelling (`1.0`, `1e0`) in an integer field; `seq` on a D→S-only type, `ping`, `pong` or `welcome`, and an S→D-only type (`welcome` excepted) without `seq` (the schema refuses these first); a first frame that is not `hello`, and a second `hello` on one connection (session rules of `server_session.py`, the session stays up) |
| `invalid_seq` | `seq` on a device-sent `error`, or on an S→D-only type sent by the device (the schema requires `seq` there, so only the direction rule can refuse it; a `welcome` has no `seq`, so the direction rule refuses the one without) |
| `field_too_long` | a string over its UTF-8 byte limit (limits table) |

## Limits and validation notes

**Frame caps** (bytes of the UTF-8 WebSocket payload): 64 KiB (65536 B) for every frame, checked by the codec
*before* JSON parsing; 32 KiB (32768 B) for `audio.chunk` and `meeting.audio`, checked before parsing when the raw
text spells the top-level `"type"` key and value verbatim, and after parsing when they are spelled with `\u`
escapes (the escaped spelling is refused there, so it gains nothing). Both refusals are `frame_too_large`.

**String limits are UTF-8 byte limits.** Both stacks measure a string as the number of bytes of its UTF-8 encoding
(Python `len(s.encode("utf-8"))`, Kotlin `s.encodeToByteArray().size`) — never code points, never UTF-16 units
(`String.length`), never graphemes. The codec (T1.06 on the server, the Android codec on the phone) enforces them
after parsing. JSON Schema cannot count bytes: each `maxLength` in the schema is the same number used as a
**code-point** bound, which is conservative (a string within N bytes has at most N code points, so the schema never
rejects a string the byte rule accepts). Producers truncate on a code-point boundary.

| field | max UTF-8 bytes |
|---|---|
| `msg.in.text` | 2000 |
| `msg.out.text` | 2048 |
| `msg.out.text` with `longRef` | 512 |
| `msg.out.longRef.fullText` | 4096 |
| `approval.request.summary` | 300 |
| `approval.request.detail` | 4000 |
| `clarify.request.question` | 2000 |
| `clarify.request.choices[]` | 200 |
| `clarify.response.answer` | 2000 |
| `meeting.start.prepTerms[]` | 100 |
| `meeting.hint.text` | 180 |
| `error.message` | 500 |
| `skill.result.error` | 500 |
| `hello.appVersion`, `hello.glasses.model`, `hello.glasses.fw`, `meeting.state.provider` | 64 |
| `audio.chunk.chunk`, `meeting.audio.chunk` (base64, ASCII) | 31744 |

**Every schema-valid frame fits its cap**, even when the serialiser escapes every non-ASCII character as `\uXXXX`
(12 bytes for an astral code point such as an emoji; 6 bytes for an escaped control character). The test
`test_worst_case_frame_fits_cap` derives the worst case per type from the schema itself. Assumption: printable ASCII
in patterned fields (ids, lanes, base64) is not escaped. Exception: the open objects `skill.call.args`,
`skill.result.output` and `skill.manifest.skills[]` items are bounded only by `maxProperties` at their top level;
their size is bounded by the frame cap and their nesting depth by the codec (max depth 32).

Other rules:

- Strings use `maxLength`, arrays `maxItems`, numbers `maximum`; open objects use `maxProperties`.
- Every `$`-anchored `pattern` also carries `"not": {"pattern": "[\\n\\r\\u0085\\u2028\\u2029]"}`: Python `re` lets `$`
  match before a trailing `\n`, and `java.util.regex` (without `UNIX_LINES`) also before `\r`, `\r\n`, `\u0085`,
  `\u2028`, `\u2029`.
- Display fields of approval and clarify cards (`approval.request.summary`, `.detail`, `clarify.request.question`,
  `.choices[]`) reject Unicode bidi control characters (U+061C, U+200E, U+200F, U+202A–U+202E, U+2066–U+2069), so
  text on the glasses cannot be visually reordered to disguise what is being approved. Other text fields accept them.
- `format` is not used (not asserted by default validators). Timestamps use a `pattern` that checks RFC 3339 field
  ranges: month 01–12, day 01–31/30/29 by month (Feb 29 in a non-leap year is left to the codec), hour 00–23,
  minute and second 00–59 (no leap second), 1–9 fraction digits, offset `Z` or `+hh:mm`/`-hh:mm` up to ±14:00.
- The codec also rejects strings that are not valid Unicode scalar values (lone surrogates).
- JSON parsing is strict: a key repeated in one object is `invalid_json`, because parsers differ on which duplicate
  wins (a parser differential: the phone and the server could read different frames); so are `NaN`/`Infinity` and
  a number literal too large for a finite float (`1e999`). Integer fields (`v`, `seq`, `durationMs`, ...) must be
  integer literals: `1.0` or `1e0` passes the schema but is `invalid_frame`. Python's `json` also refuses a control
  character below U+0020 inside a string, a bare value that is not `true`/`false`/`null`/an RFC 8259 number (`ptt`,
  `01`, `+1`, `'x'`), an integer literal of more than 4300 digits (the interpreter's default limit) and whitespace other than space, tab, LF and CR.
- Kotlin decoder: the phone reads frames only with the generated `decodeEnvelope` (`Envelope.kt`), which refuses
  everything in the previous bullet and the nesting rule below (more than 35 open brackets), an explicit `null`, a
  quoted number or boolean, a non-integer spelling and an integer outside its schema bounds; `1e999` is refused (not a
  finite number) and `1e-999` is accepted, as in Python. Both stacks run the `raw-invalid/` and `lexical.json` cases.
  It is **not** equivalent to the Python codec in these known gaps (the `invalid/` and `codec-invalid/` fixtures it
  still accepts are listed by rule in the Kotlin tests, and a test fails when one starts or stops being refused):
  lane pattern, id pattern, base64 pattern, timestamp pattern, bidi control characters, string length, conditional
  keywords, seq direction, UTF-8 byte limit, calendar of ts, lone surrogate, depth 32 of an open field, and the frame
  size cap (the transport's job). Its error messages differ from the codec's.
- Nesting depth of the open objects `skill.call.args`, `skill.result.output` and each `skill.manifest.skills[]` item
  (codec, max 32) is counted from the named value: the value itself (the `args` object, the `output` object, one
  `skills[]` item) is level 1, each nested object or array adds 1, scalars add none; depth 33 is rejected
  (`frame_too_deep`). This per-field count is authoritative. The codec's raw pre-scan of the whole frame is only a
  coarse recursion guard and uses the largest enclosing offset (envelope object, `body` object, `skills` array = 3),
  so it allows up to 35 nested open brackets (`{`/`[` outside strings) for any frame and rejects 36. Fixtures:
  `s2d-skill-call-args-depth-32/33`, `d2s-skill-result-output-depth-32/33`, `d2s-skill-manifest-item-depth-32/33`.
- `msg.out.longRef.truncated` (boolean, optional, requires `fullText`): `true` means `fullText` holds only the start
  of the answer (cut at 4096 B on a code-point boundary) and the phone shows `truncated — full text in <channel>`;
  absent means `fullText` is complete or absent. Producers omit `truncated` rather than send `false`; consumers
  accept both (absent and `false` both mean complete).

## HTTP `/enroll`

Enrolment is the one HTTP call of this protocol. It happens before the device has a certificate, so it is not a
WebSocket frame and carries no envelope. The phone sends it to the plugin's `/enroll` route; the plugin checks it and
relays it to the device-CA signer, which owns the enrolment window, the one-time code, the proof check and the
issuance. Status: the relay is built (`hermes-plugin/glasses/enroll.py`) and mounted by the WebSocket server's app for every method
(`hermes-plugin/glasses/server.py`, T1.07c; a peer the server refuses gets the unknown-path `404` for every method, and
`/enroll` never upgrades to a WebSocket); it is served on the edge's unix socket only (T1.184b); the phone's request and answer rules are built (pure, `android/protocol/.../enroll/`, T1.23a, T1.23c); the Keystore proof of possession (T1.22) and the enrolment screen and client (T1.23b) are planned.

| part | rule |
|---|---|
| request | `POST /enroll`; the phone sends no other method (the server answers any other one with `405`) |
| `Content-Type` | `application/json` |
| body cap | at most `4096` bytes; more is answered `413` |
| body | one strict JSON object: UTF-8, no repeated key, no `NaN`/`Infinity`, exactly the keys below, every value a string, no other key (a name, lanes or device info are refused) |

| key | value |
|---|---|
| `spki` | the SPKI DER of the device's public key (1..256 bytes) as canonical padded base64: standard alphabet, `=` padding, no line breaks |
| `code` | the 22-character base64url code from the enrolment payload, as received |
| `signature` | the DER ECDSA P-256 / SHA-256 signature (1..80 bytes), canonical padded base64, proving possession of the key over `b"wayfinch-enroll-v1\x00"` ‖ `code` (ASCII) ‖ the SPKI DER |

The relay forwards `signature` to the signer as `sig` (the signer's field name; the phone never sends `sig`). A value
in any other encoding is refused with `400`.

| status | meaning |
|---|---|
| `200` | JSON `{"cert", "token", "deviceId"}` with `Cache-Control: no-store`. The answer can take up to 340 s: the signer holds the call until the operator confirms |
| `400` | one generic answer for any bad request: wrong content type, a body or field that is not as above, a bad proof, a key that is already enrolled |
| `404` | refused (for example no enrolment window is open): the same bytes as an unknown path |
| `413` | body over the cap |
| `429` | rate limited (6 a minute in total) or two enrolments already running |
| `503` | the signer is unavailable, or any other failure |

Bodies of non-`200` answers are fixed texts. The phone acts only on the status code and, on `200`, on the three keys.

## Tests

`make test-protocol` (fragment `mk/protocol.mk`), or directly:
`.local/venv-dev/bin/python -m pytest protocol/tests -q`.
