<!-- SPDX-License-Identifier: Apache-2.0 -->
# Wayfinch protocol — envelope v1

## Purpose

`envelope.schema.json` (JSON Schema draft 2020-12) is the contract between the phone app (**device**, D) and the
server plugin (**server**, S). Every WebSocket text frame is one JSON object (an *envelope*). Both stacks validate
frames against this schema; golden fixtures live in `fixtures/` (T1.03).

## Public API

- `envelope.schema.json` — the contract (JSON Schema draft 2020-12): the envelope, the `type` enum and one
  `$defs/body.<type>` per frame type.
- `fixtures/` — golden frames (`valid/`, `invalid/`, `codec-valid/`, `codec-invalid/`) both stacks test against.
- This document: the field, message-type, close-code and error-code tables below, the delivery rule, and the
  limits table (UTF-8 byte limits per field) in *Limits and validation notes*. Tests and `tools/codegen/` parse the
  message-type and limits tables, so their row format is part of the API.

## Invariants

- One WebSocket text frame = one envelope object; no top-level field outside the envelope table, and every body
  schema has `additionalProperties: false` and a bound on every field — except the open objects
  `skill.call.args`, `skill.result.output` and `skill.manifest.skills[]` items, bounded only by `maxProperties`
  (see *Limits and validation notes*).
- String limits are byte limits, which the codecs must enforce: the server-side codec
  (`hermes-plugin/glasses/envelope.py`, `envelope_encode.py`) enforces them on every frame it decodes or encodes
  (the WS server that uses it is planned in T1.07; the Android codec is planned); the `codec-valid/` and
  `codec-invalid/` fixtures fix the behaviour. The schema's `maxLength` is the same number as a code-point bound,
  so the schema never rejects what the byte rule accepts.
- Schema, these tables and the fixtures change together; the generated plugin types are regenerated
  (`make check-codegen`) and `make test-protocol` stays green.
- Delivery rule: S→D-only frames carry `seq` and stay outboxed until acked; D→S frames never carry `seq` and are
  processed at most once (Delivery). The server-side codec enforces the `seq` rules (the WS server that uses it is
  planned in T1.07); the outbox (T1.09) and the dedupe by `id` (T1.08) are planned.

## Envelope

| field | required | meaning |
|---|---|---|
| `v` | yes | protocol major, `1`. A mismatch ⇒ `error` `unsupported_version` + close `4400`. |
| `id` | yes | ULID, unique per sender. D→S frames are processed at most once (dedupe by `id` within 10 min). |
| `type` | yes | one of the types below; selects the `body` sub-schema. |
| `ts` | yes | RFC 3339 date-time with offset (`Z` or ±hh:mm ≤ ±14:00), e.g. `2026-10-03T18:00:00Z`; field ranges are checked (see Limits). |
| `seq` | S→D | per-device monotonic integer 1…2^53−1, acked by the device. **Required** on S→D-only types; **forbidden** on D→S-only types and on `ping`/`pong`; **optional** on `error` only (rules in Delivery). |
| `corr` | no | ULID of the frame this one answers. |
| `lane` | no | lane name, pattern `^[a-z][a-z0-9_-]{0,31}$`. A pattern, not a list: which lanes exist is server config (examples here use `assistant` and `work`). |
| `body` | yes | type-specific object; every body schema has `additionalProperties: false` and bounds on every field. |

No other top-level field is allowed. Field names in the design marked `?` are optional; all others are required.

## Message types

`B` = UTF-8 bytes (see Limits).

| type | dir | body (required, `?` = optional) |
|---|---|---|
| `hello` | D→S | `appVersion`, `protocol`, `lastAckedSeq`, `manifestHash`, `glasses{model, fw}` |
| `welcome` | S→D | `deviceId`, `serverTime`, `lanes[]` (≤32), `limits{maxFrame, maxAudioSec}`, `resumeFrom` |
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

- **S→D-only types** always carry `seq` and stay in the server outbox until acked (`ack.upToSeq`; TTL 24 h, max 500
  per device; overflow drops the oldest `push` and sends `error` `outbox_overflow`; pending clarify cards are exempt
  from TTL and eviction).
- **`ping` / `pong`** (keepalive, both directions) never carry `seq` and are never outboxed. The schema rejects `seq`
  on them.
- **`error`** is the only type where `seq` is optional. An S→D `error` that reports a state the device must not miss
  (e.g. `outbox_overflow`) carries `seq` and is outboxed like any S→D frame; a connection-scoped S→D `error` sent just
  before a close (e.g. `unsupported_version`) has no `seq` and is not outboxed. A D→S `error` never has `seq`.
- **D→S frames** never carry `seq`; they are processed at most once (dedupe by `id` within 10 min).

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
| `4409` | `replaced` | a newer connection for the same device took over (one connection per device). The client must not reconnect automatically sooner than 60 s and shows "connected elsewhere"; an app instance that itself holds the newer connection just drops the old socket |
| `1000` | `normal` | standard RFC 6455 normal close |
| `1001` | `going_away` | standard RFC 6455: server shutdown / restart |
| `1009` | `message_too_big` | standard RFC 6455: frame over the size limit |

Authentication that fails **before** the upgrade (the server checks credentials on the HTTP upgrade request) is
answered with HTTP `401` (missing/invalid credentials) or `403` (revoked device) and never reaches a close code; the
`44xx` codes are for failures after the upgrade.

## Error codes used so far

`unsupported_version`, `outbox_overflow`, `lane_forbidden`, `lane_unrouted`, `provider_down` (meeting state).
New codes must match `^[a-z][a-z0-9_]{0,63}$`.

The server-side codec (`hermes-plugin/glasses/envelope.py`) refuses a D→S frame with one of the codes below; its
`error_frame` builds the matching `error` frame without `seq` or `corr` (the WS server that sends it is planned in
T1.07). The checks run in the order of the module docstring; the first one that fails decides the code. The
`message` is fixed per code and never echoes input.

| code | rule that yields it |
|---|---|
| `frame_too_large` | payload over its frame cap (see *Frame caps* below) |
| `frame_too_deep` | the raw nesting pre-scan, or an open field (`skill.call.args`, `skill.result.output`, a `skill.manifest.skills[]` item) nested deeper than 32 |
| `invalid_text` | bytes that are not UTF-8; a string or key that is not Unicode scalar values (lone surrogate) |
| `invalid_json` | not one JSON text; `NaN`/`Infinity`; a number literal too large for a finite float (`1e999`); a key repeated in one object |
| `invalid_frame` | fails the envelope schema; Feb 29 of a common year; a non-integer spelling (`1.0`, `1e0`) in an integer field; `seq` on a D→S-only type, `ping` or `pong`, and an S→D-only type without `seq` (the schema refuses these first) |
| `invalid_seq` | `seq` on a device-sent `error`, or on an S→D-only type sent by the device (the schema requires `seq` there, so only the direction rule can refuse it) |
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
  integer literals: `1.0` or `1e0` passes the schema but is `invalid_frame`.
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

## Tests

`make test-protocol` (fragment `mk/protocol.mk`), or directly:
`.local/venv-dev/bin/python -m pytest protocol/tests -q`.
