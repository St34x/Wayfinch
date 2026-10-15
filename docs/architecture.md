<!-- SPDX-License-Identifier: Apache-2.0 -->
# Architecture

This document describes the target architecture of Wayfinch: what runs where, how data flows, how it is secured, and
why. **Most of it is not built yet.** The README's [Status](../README.md#status) section lists what exists today.
Terms follow [glossary.md](glossary.md). Wire-level details are in [protocol/README.md](../protocol/README.md).

## 1. Components

```mermaid
flowchart TB
    subgraph Glasses["G2 glasses (vendor firmware)"]
        FW["display · temple touchpads · mic · IMU · built-in wake engine · battery"]
    end
    subgraph App["Android app"]
        DRV["glasses: BLE driver + G2Page API"]
        HUD["hud: composer + scheduler (pure)"]
        INP["input: gesture reducer (pure)"]
        VO["voice: wake / push-to-talk capture"]
        LINK["link: WSS client, mTLS, acked inbox"]
        SK["skills + confirm + consent"]
        UI["companion · onboarding · emergency settings"]
    end
    subgraph Srv["Server"]
        PX["Reverse proxy (deployer's own)"]
        subgraph GW["Hermes gateway process"]
            PL["glasses plugin: WS server, registry, codec, outbox, cards, long-answer router"]
            BR["phone bridge (MCP, loopback)"]
            ME["meeting engine (later)"]
            AG["agent profiles"]
        end
        ADM["glasses-admin CLI"]
    end
    CH["Chat channel"]
    SP["Streaming STT provider (e.g. Soniox)"]
    STT["Gateway's configured STT provider (fallback)"]
    FW <-->|BLE| DRV
    DRV --- HUD & INP & VO
    VO -->|"command audio, short-lived token"| SP
    LINK <-->|"WSS / TLS / mTLS"| PX
    PX <-->|loopback| PL
    PL <--> AG
    BR <--> AG
    PL -.->|"fallback clips"| STT
    AG -.-> CH
    ADM -->|"unix admin socket"| PL
```

**Glasses.** The G2 firmware owns the display (576×288, firmware fonts), the touchpads, the microphones, the IMU and a
built-in wake-phrase engine. The glasses have no speaker and no camera. Wayfinch ships no firmware and no vendor assets.

**Android app** (Kotlin, planned). One app with these modules:

| Module | Responsibility |
|---|---|
| `glasses` | The BLE link through a vendored copy of the open-source (Apache-2.0) G2 driver layer from MentraOS, behind our own `G2Page` / settings / mic API. No third-party UI and no analytics code. Connects only to the enrolled glasses |
| `hud` | Pure Kotlin. Fixed layout skeletons, glyph and width budgets, text wrapping and paging, a small animated finch face, and a scheduler that turns screen changes into the fewest display operations within the BLE budget |
| `input` | Pure state machine: gestures from the touchpads → UI actions (idle / listening / answer / card) |
| `voice` | Push-to-talk and the glasses' wake event. The mic is on only from the wake/press until end of speech or 30 s. The audio streams from the phone directly to the streaming STT provider with a short-lived token from the plugin, and only the final text goes to the plugin. Fallback, when streaming STT is not configured or not available: the clip goes to the plugin as Opus chunks while capturing |
| `link` | One persistent WSS connection: TLS with a client certificate from the Android Keystore, a per-device token, a pin on the server's CA set, reconnect with backoff, heartbeat, an acked inbox and replay |
| `skills`, `confirm`, `consent` | Phone-skill registry and executor; the single authority for side-effect confirmations; biometric-bound consent for widening access |
| `companion`, `onboarding`, `emergency` | A full-screen companion (the finch face mirroring the state), the setup steps Android forces on-device, and emergency settings behind the device credential |

The phone has no typing UI. Configuration happens by voice or through the agent's chat channel, within the limits in
§4.

**Server.** Everything server-side runs inside the deployer's Hermes gateway; the only separate service is the device-CA
signer (planned, built outside this repository). Built
today: the plugin's platform registration, configuration check (`config check`), the device CRL commands (`crl` asks
the device-CA signer to generate and publish it, `crl --check`; the plugin holds no CA key), `revoke` (the signer's
tombstone and CRL, then the plugin's admin socket), the signer client, the `/enroll` relay (not mounted on any HTTP
server yet), device registry and allow-all gate,
the server-side envelope codec (a library no server calls yet), and an adapter skeleton that fails closed (it reports "not connected" and delivers nothing). Everything else in this
table is planned.

| Component | Responsibility |
|---|---|
| Reverse proxy | The deployer's own nginx, Caddy or similar. It terminates TLS, requires on `/ws` a client certificate and verifies it against the **device CA only**, forwards the certificate to the plugin in a header (stripping any client-supplied copy), and proxies `/ws` and `/enroll` (always routed: the signer refuses an enrolment while no window is open) |
| `glasses` plugin | A Hermes gateway platform plugin. Loopback-only WebSocket server, one connection per device; credentials checked at the HTTP upgrade; device registry; envelope codec; short-lived speech-to-text tokens minted per command or meeting session (the long-lived provider key stays here); inbound text (incl. the final text of a command) and fallback voice clips → gateway message events; `send()` → notice classification → long-answer router → durable outbox with sequence numbers and acks; approval and clarify cards; HUD state; audit log |
| Phone bridge | Planned (Phase 2). An MCP endpoint inside the plugin on a second loopback port. It offers four stable meta-tools (list skills, describe a skill, read, act) and forwards validated calls to the phone over the existing connection |
| `glasses-admin` | Operator CLI. Today it has `config check` (`--live` also checks the host and the routed Hermes profiles; it writes nothing into the Hermes home, reads `<home>/.env` (a regular file of at most 1 MiB, never rewritten; names that load code or move directories, such as `PATH`, `LD_*` or the plugin switches, are not taken from it, and the plugin discovery never sees it) for the `${VAR}` refs, `HERMES_LOCAL_STT_LANGUAGE` and the `allow_all_users` grant of `config.yaml` (top level or under `gateway:`) the gateway would see, prints no `.env` value of four characters or more and no value that a `${VAR}` puts into a timeout or into the whole name of a routed profile (a shorter `.env` value, or a process-environment value, inside a longer profile name can still be printed, and so can a `.env` value padded with white space inside quotes, `NAME=" value "`, when it is used as a whole route profile name: Hermes strips and lower-cases that name, the mask and the hiding compare the raw value, so it is printed in its stripped, lower-cased form; tool names outside Hermes' toolset table are only counted, profile names must be plain identifiers, Hermes' own log output is silent), and refuses, exit 2, a home the effective user does not own; plugins installed under a profile's own home are not discovered by this check, so tool parity is computed from the config only) and `crl` / `crl --check` (`crl` asks the device-CA signer over `signer.socket` to generate and publish the device CRL and prints one line per CA; a signer that is down, slow or refuses is one fixed error line and exit 1; the plugin holds no CA key and never signs; `crl --check` checks the CA and CRL bundles the signer publishes in `signer.public_dir`: it warns 14 days before a CRL and 180 days before a CA expires, exits 1 on errors and 3 on warnings only, and never reads the edge copy) and `revoke DEVICE_ID` (asks the signer's `revoke` op for the tombstone and the CRL, then the running plugin over `$state_dir/admin.sock`; exit 0 only when both confirm; the admin-socket server and the live cut are planned, T1.63a). The signer itself (device CA, issuance) is a separate service, planned; enrolment windows are opened only from the root side, never by this CLI. Planned: device list (T1.15f), the live cut (T1.63a), the plugin's daily `crl --check`, and server pins. Command reference: [admin-cli.md](admin-cli.md) |
| Meeting engine | Later (Phase 4). It mints a short-lived provider token per meeting session (the phone streams the audio to the streaming STT provider directly, with speaker labels), receives the transcript text from the phone, and runs tool-less LLM passes for hints, notes and the summary. It never receives meeting audio |

## 2. Data flows

None of these flows is built yet; this section describes the target design.

### 2.1 Voice command → answer on the glasses

```mermaid
sequenceDiagram
    participant G as Glasses
    participant A as App
    participant S as Streaming STT provider
    participant P as Plugin
    participant H as Gateway + agent
    G->>A: wake event (built-in engine) or long press
    A->>P: request a speech-to-text token
    P-->>A: short-lived, single-use token
    A->>G: "listening" screen, mic on
    G-->>A: mic audio (until end of speech, ≤30 s)
    alt default: streaming speech-to-text from the phone
        A->>S: audio stream (TLS, token)
        S-->>A: live words, final text, end of utterance
        A->>P: msg.in (final text, lane)
        P->>H: text message event, agent answers
    else fallback: streaming not configured or not available
        A->>P: audio.start / audio.chunk… / audio.end (lane)
        P->>H: voice message event (temp file, deleted after)
        H->>H: transcribe with configured STT, agent answers
        H-->>P: transcript echo
        P->>A: msg.out kind=heard
    end
    H->>P: send(answer)
    P->>A: msg.out kind=answer (seq)
    A->>P: ack upToSeq
    A->>G: answer, paged
```

Nothing is captured or sent outside this window, except in meeting mode, which the user starts explicitly.

By default the command audio goes from the phone straight to the streaming STT provider over TLS, and only the final
text reaches the server. The phone holds only a short-lived, single-use token that the plugin mints for this one
command, and only for a registered, non-revoked device. The long-lived provider key stays on the server. The token
frames are planned additions to the protocol (§3). The **fallback** clip path through the plugin and the gateway's
configured STT provider is used only when streaming STT is not configured on the server or not available (token
refused, provider unreachable, stream failed, daily usage cap reached). A revoked device gets no token and sends no
fallback clip. On the fallback path, answer latency depends mainly on the deployer's STT provider and model.

### 2.2 Long answers → chat channel

When a reply is longer than a configurable threshold (default 280 characters, never more than 2048 UTF-8 bytes), or
contains a table, code, several links or an attachment, the plugin does two things. It sends the glasses at once a
one-line pointer with the first sentence (≤512 B) and a phone-side copy (≤4096 B). Its outbox worker then delivers the
**complete** text asynchronously to the home channel of the profile that owns the lane (default platform: Telegram).
Delivery status (`sent` / `retrying` / `failed`) follows as `long.status`. A permanent failure leaves the answer saved
on the phone. The agent needs no tool for this: the plugin is the only mechanism.

### 2.3 Slow work and later pushes

A fast front-desk profile answers short questions itself. Longer jobs go to other profiles through the agent
framework's own task hand-off. The glasses show a short "handed off" acknowledgement. Later results, blocked tasks
and notices arrive through `send()`, are classified (agent reply, task notice, system notice, transcript echo) and
pushed as `msg.out`. While the phone is offline they wait in the outbox (TTL 24 h, at most 500 per device) and are
replayed after `hello.lastAckedSeq` on reconnect.

### 2.4 Approvals and clarifying questions

The plugin overrides the gateway's approval and clarify prompts:

- **Exec approvals** become `approval.request` cards. The only choices are `once` or `deny`, *deny* is preselected,
  confirming takes two steps, and expiry fails closed. Card text is cut on code-point boundaries, and bidi control
  characters are rejected so the text cannot be visually reordered.
- **Clarifying questions** never expire. If the agent moves on, the card stays as a *detached* question, and answering
  it later starts a new turn. Only an explicit dismissal or a revocation removes it.

### 2.5 Phone skills (Phase 2)

An agent calls the bridge's meta-tools. The bridge validates the arguments against the device's skill manifest and
sends `skill.call`. The phone validates again, checks that the skill is enabled and its permission granted, and, if
the skill has side effects, renders the exact action from its own template on the glasses and as a phone
notification. A single-use nonce, bound to the call and an argument hash, must be confirmed within 120 s. Rejections
and timeouts return `denied` / `expired`. A write that times out is reported as uncertain and never retried.

### 2.6 Meeting mode (Phase 4)

Meeting mode starts only by an explicit user action and only on devices with the `meeting` feature. The glasses show a
reminder to announce it, and the phone shows a persistent "recording" notification. Glasses-mic audio streams from the
phone directly to a streaming STT provider with speaker labels, using a short-lived token that the plugin mints for
that meeting session. The long-lived provider key stays on the server and never reaches the phone. The phone sends
the transcript text to the plugin, and the server never receives meeting audio. Tool-less LLM passes turn the rolling
transcript into short hints
(one at a time on the glasses), periodic notes, and a final summary and transcript delivered to the chat channel.
**No audio is stored anywhere.** Transcript text is kept on the server for a limited, configurable time. Meeting
participants are untrusted input: their speech never reaches an agent session that has tools.

## 3. Protocol overview

The app (device, D) and the plugin (server, S) exchange JSON *envelopes* over one WebSocket. The single source of truth
is [`protocol/envelope.schema.json`](../protocol/envelope.schema.json) (JSON Schema 2020-12). Its rules are documented
in [`protocol/README.md`](../protocol/README.md). Python types are generated from it today; Kotlin types will be
generated from it with the Android app. The server-side codec that enforces these rules on every frame it
decodes or encodes is built (`hermes-plugin/glasses/envelope*.py`); the WebSocket server that will call it, the
outbox, the dedupe by `id` and the Android codec are planned.

- **Envelope:** `v`, `id` (ULID), `type`, `ts`, optional `seq`, `corr`, `lane`, and a strict `body` (no extra fields;
  every field bounded).
- **27 frame types:** session (`hello`, `welcome`, `ping`, `pong`, `ack`, `error`), messages (`msg.in`, `msg.out`,
  `long.status`), voice (`audio.*`: the fallback clip path; the frames that hand the phone a speech-to-text token
  are planned), cards (`approval.*`, `clarify.*`, `card.state`), HUD state (`hud.state`), phone
  skills (`skill.*`) and meeting mode (`meeting.*`).
- **Delivery:** server-to-device frames carry a per-device `seq` and stay in the outbox until acked. Device-to-server
  frames are deduplicated by `id`. Keep-alives are never queued.
- **Limits:** frame caps are checked before parsing (64 KiB; 32 KiB for audio). String limits are **UTF-8 byte**
  limits. A test proves that every schema-valid frame fits its cap even with worst-case escaping.
- **Close codes:** `4400` unsupported version, `4401` unauthorised, `4403` revoked, `4409` replaced by a newer
  connection. Authentication failures before the upgrade are plain HTTP `401`/`403`.

## 4. Security model

An enrolled device can ask agents that may have powerful tools to act, so **a device credential is treated as
privileged**. Every control below follows from that.

This section is the target design. Built today: device tokens stored only as SHA-256 hashes and verified in constant
time, revocation tombstones in the registry, a registry that fails closed on a corrupt or foreign-owned file, refusal
of allow-all or wildcard user settings, a `host` setting that accepts loopback addresses only, the client of the
device-CA signer's socket (owner and peer-uid checks, no CA key in the plugin), the plugin's side of enrolment (the
`/enroll` relay: request checks and a rate limit, then the signer's `enrol` op; no HTTP server mounts it yet), the
signer-then-admin-socket client of `glasses-admin revoke`, and the pinned secret
scan in `make test`. Everything else here (the signer service itself, the transport and its authentication, mTLS, the
enrolment window and the app's enrolment, the plugin's side of the admin socket and the live cut, the phone bridge, the
app's controls, CI and release signing) is planned.

| Boundary | Controls |
|---|---|
| Glasses ⇄ phone (BLE) | Connect only to the enrolled glasses; the mic is off unless listening, with a visible indicator; secrets are never drawn on the display |
| App ⇄ other apps | No exported components beyond the launcher, immutable notification intents and the foreground service; no local servers; settings behind the device credential |
| Third-party code in the app | Vendored driver reviewed at a pinned commit and on every sync; dependencies pinned with checksums (Gradle dependency verification); no analytics code, enforced by a guard and an egress test; licence check |
| Phone ⇄ proxy (internet) | TLS, with mTLS against a dedicated device CA on `/ws` (`/enroll` cannot require one: the device has none yet); client key non-exportable in the Keystore; server trust = system anchors + a CA-set pin (Certificate Transparency stays on); rate limits; only `/ws` and `/enroll` exposed (`/enroll` always routed and rate-limited; without an open enrolment window the signer refuses it) |
| Phone ⇄ streaming STT provider (internet) | TLS to the provider's host only, while a command capture or a meeting is open; the phone holds only short-lived, single-use, scope-limited tokens in memory (never stored or logged), minted by the plugin per command or meeting session and only for non-revoked devices; the long-lived provider key never leaves the server; the provider sees the phone's IP address |
| Proxy ⇄ plugin (loopback) | The plugin binds `127.0.0.1` only; it checks the bearer token (constant-time, on SHA-256) **and** that the forwarded certificate's fingerprint matches the registry |
| Plugin ⇄ agents | Lanes allow-listed per device; glasses approvals limited to once/deny; inbound text treated as user input; skill outputs wrapped as data, never instructions |
| Agents ⇄ phone bridge | Loopback bind and a separate bearer secret; configured only in the profiles the deployer lists |
| Chat channel ⇄ agents | Requests from chat can only **narrow** phone access; **widening** needs a biometric-bound consent signature made on the phone |

**Enrolment (planned, not running yet).** The device CA key and certificate issuance live in a separate signer service
under its own user, built and reviewed by the operator's admin agent outside this repository (milestone M3); the plugin
holds no CA key and asks the signer over its unix socket. Only the root side opens an enrolment window: the operator
from a root-side confirmation card on his phone, the admin agent at his request, or the signer's root CLI - never
`glasses-admin`, the plugin or an agent. The window's payload (URL, one-time code, server pins) goes to the operator as
an app link or QR text. The app generates its key in the Android Keystore and sends the public key with a
proof-of-possession signature to `/enroll`; the plugin relays it to the signer (`enroll.py`, built: request checks, rate
limit, then the signer's `enrol` op; not mounted on an HTTP app until T1.07). The signer issues a
short-lived certificate and a 256-bit token only after the operator confirms the key fingerprint on a root-side card;
the code is single-use, and the window closes after use. Certificates are renewed over the authenticated channel.

**Revocation is immediate and does not depend on the proxy.** `glasses-admin revoke DEVICE_ID` (built, T1.15e) first
asks the signer's `revoke` op to write the tombstone and the new CRL (the CLI writes no file itself), then calls the
running plugin over the unix-domain admin socket `$state_dir/admin.sock` (one JSON line each way,
`glasses.admin_protocol`; the client refuses a path that is not a socket owned by its own user or a peer of another
uid). It exits 0 only when both confirm; any signer failure, `unknown_device` included, still calls the socket (the cut
only narrows access) and then exits 1. A config with errors stops `revoke` before both steps (neither the signer nor
the plugin is asked): fix the config first (`glasses-admin config check`), or the open session stays until a gateway
restart. The
socket server and the live cut below are planned (T1.63a): the plugin then kills the token, keeps the fingerprint
only as a tombstone, closes the connection with `4403` within one second, purges the outbox, drops unhandled inbound
frames and in-flight voice, denies pending approvals, cancels clarify cards, closes phone-bridge sessions, stops
minting speech-to-text tokens and stops a running meeting. On the revocation signal the app closes any open provider
stream, wipes its tokens and sends no fallback clip. Until then, a revocation the signer confirmed refuses new
connections (tombstone and CRL), but an already open session stays until the plugin's live cut (planned, T1.63a /
T1.179) or a gateway restart; `revoke` says so when the socket leg is not confirmed. A file-change backstop that
applies a revocation even if the socket was unreachable is planned (T1.63a / T1.179). A regenerated CRL for the proxy
is optional extra hardening.

**Secrets and logs.** Nothing secret is in git (`make test` runs a pinned gitleaks scan). Tokens are stored only as
hashes, the plugin holds no CA key (it stays with the separate signer service under its own user; planned, M3), and
long-lived provider keys stay on the server in the gateway's environment (supplied
from a secret manager, not a plaintext file) and never reach the phone. Logs and audit entries carry ids,
counts and sizes, never tokens, certificates, audio, transcripts or message text.

**Fail closed.** An unknown state, an expired approval, an unreadable or corrupt config/registry, a wildcard or
allow-all user setting, or a non-loopback bind is refused.

**Supply chain and CI (planned for the public repo).** CI runs on `pull_request` only, with read-only permissions and no
repository secrets, and actions are pinned by SHA. Release APKs are signed offline by the maintainer and published
with checksums and the signing-certificate fingerprint.

## 5. Key design decisions

| Decision | Why | Rejected |
|---|---|---|
| Own Android app on direct BLE, no vendor phone app or cloud in the data path | Full control of the UI and the data path | Vendor plugin ecosystems (need the vendor app and cloud) |
| Vendored, Apache-2.0 G2 driver layer behind our own API | Stable API, no third-party UI or analytics, upstream fixes still reachable by diff | Shipping a full SDK with its UI and analytics; writing our own driver from scratch |
| Hermes **gateway platform plugin** | The only interface that has push, approvals, clarify, scheduled/hand-off delivery and profile routing in one place | The agent's HTTP API (no push, no clarify, unattended approvals) |
| Phone bridge as MCP with four stable meta-tools inside the plugin | One phone socket and one auth system; a fixed tool list (no schema churn per skill) | One MCP tool per skill; a separate bridge process |
| Built-in G2 wake phrase; push-to-talk always available | No phone-side wake engine, licence or model; no always-on phone mic | Phone-side keyword spotting |
| Command STT streamed from the phone directly to a streaming provider with a short-lived token from the plugin; the gateway's **existing** STT path (voice message events) as the fallback | Live words while speaking and a semantic end of utterance, one hop less; the long-lived key stays on the server; deployers without a streaming provider keep whatever STT they already use | Relaying the stream through the server; batch STT only; a dedicated STT service; on-phone STT |
| Persistent WSS with an acked outbox; no push service in v1 | No third-party cloud or server key in the trust path | Push-notification wake-ups (possible later) |
| Long answers to the owning profile's chat channel, routed by the plugin | The glasses show short text only; the chat is an existing reading surface | Paging long text on the glasses |
| Phone-rendered, nonce-bound confirmations | The agent cannot word or forge what the user approves | Relying only on the agent framework's approval policy |
| Ports and adapters with a pure core; enforced import boundaries | Deterministic, fast tests; adapters stay thin | Business rules inside socket handlers |
| Schema-driven codegen from one JSON Schema | One contract for both stacks; drift fails the build | Hand-written types; generic generators that cannot express the body dispatch |
| Fail-closed defaults everywhere | A misconfiguration must never grant access | Permissive defaults with warnings |
| Deployer's own reverse proxy; no proxy-less mode | TLS and client-cert handling stay in proven software; the plugin stays loopback-only | Exposing the plugin directly |
| Config-driven deployment, no host-specific values in code | Others can deploy it; examples use `example.com` | A single-operator runbook |
| Meeting audio streamed from the phone directly to the provider with a short-lived token minted by the plugin; no audio stored | Lower latency; the long-lived key stays server-side; the server never receives meeting audio; minimal retention | Relaying the stream through the server; long-lived keys on the phone |
| English UI from message catalogs | Translatable without code changes; budgets tested per locale | Hard-coded strings |

## 6. Deliberately out of scope

- Glasses other than the G2, and phones other than Android. v1 targets Android 17 (API level 37) only.
- Any server component outside the Hermes gateway: no docker-compose stack, no separate STT service, no proxy-less
  mode.
- Agent logic in the app. The agents stay the brain; the app is a front-end and a set of phone capabilities.
- A typing UI or a general settings app on the phone.
- Continuous listening outside a command or an explicitly started meeting. Wayfinch never stores meeting audio.
- Phone-side wake-word engines.
- Reverse engineering of the glasses protocol by this project, and bundling any vendor firmware, fonts or logos.
- Distribution through app stores in v1: source and signed APKs only.
