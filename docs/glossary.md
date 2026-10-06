<!-- SPDX-License-Identifier: Apache-2.0 -->
# Glossary

One term = one name. Use these names in code, docs, commit messages and UI catalogs; do not invent synonyms. Add a
term here in the same change that introduces it.

| Term | Meaning | Not to be called |
|---|---|---|
| **app** | The Wayfinch Android app on the phone | client, companion app (the companion is one screen of the app) |
| **plugin** | The `glasses` platform plugin running inside the Hermes gateway | server, backend, bridge |
| **phone bridge** | The plugin's MCP endpoint that exposes phone skills to Hermes profiles | MCP server (ambiguous) |
| **device** | One enrolled phone + app installation, identified by its device id and client certificate | client, user |
| **envelope** | One JSON frame of the phone ⇄ plugin protocol (`protocol/envelope.schema.json`) | message (ambiguous), packet |
| **frame type** | The `type` field of an envelope (`msg.in`, `approval.request`, …) | kind, op |
| **lane** | A named conversation channel on the glasses, routed to one Hermes profile | channel, topic |
| **outbox** | The plugin's durable per-device queue of server-to-device envelopes with `seq`/ack | queue, buffer |
| **long answer** | A reply too long for the glasses; delivered in full to Telegram, with a pointer on the glasses | long reply, overflow |
| **pointer** | The one-line glasses notice that a long answer went to Telegram | stub, link |
| **approval** | A time-bound allow-once/deny decision for an agent action; expires as deny | confirmation (see below) |
| **confirmation** | The phone-side check of a phone-skill side effect (single-use nonce) | approval |
| **clarify card** | A question from the agent with options; never expires | prompt, question card |
| **card** | A bordered glasses screen that needs the user (approval, clarify) | dialog, popup |
| **skeleton** | A fixed box layout of the glasses screen (`B-conv`, `B-card`, `B-crit`, `B-meet`) | template, page layout |
| **page** | The app's own EvenHub display page on the glasses (declared once, then content-only updates) | screen (ambiguous), view |
| **rebuild** | Re-creating the page layout on the glasses (blank flash, mic restart) | redraw, refresh |
| **content-only update** | Changing the text or image of an existing box without a rebuild | redraw |
| **status line** | The one-line bottom text box: `[TAG] state · detail` | footer, status bar |
| **agent tag** | The 2–4 letter callsign in brackets at the start of the status line | label, badge |
| **face** | The animated finch face bitmap (48×48, 24×24) | avatar, bird, mascot |
| **firmware dashboard** | The glasses' own home screen (time, date, battery, widgets) drawn by the glasses firmware | home screen, Even dashboard (in code) |
| **head-up** | Lifting the head past the configured angle | head tilt, look-up |
| **vendored driver** | The G2 driver source copied from upstream into `android/third_party/` and modified | SDK, library |
| **core** | Pure code without I/O (composer, reducer, codec, router decision, confirmation rules) | domain, logic layer |
| **adapter** | Code that connects the core to the outside world (BLE, WSS, Hermes, Android, Telegram) | driver (except the vendored driver), glue |
| **overlay** | A deployer's private configuration directory `deploy/local/` | local config, secrets dir |
