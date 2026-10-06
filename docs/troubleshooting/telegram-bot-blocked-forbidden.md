<!-- SPDX-License-Identifier: Apache-2.0 -->
# Long answers not delivered to Telegram: `bot was blocked by the user`

## Symptom
The glasses show the long-answer pointer as failed; the log shows the Telegram API error
`Forbidden: bot was blocked by the user` (no retry).

## Context
Long answers are delivered to the owning profile's Telegram home channel.

## Cause
The user blocked (or never started) that profile's bot; Telegram refuses the message permanently, so retrying is
pointless.

## Fix
Open the bot in Telegram, unblock it / press Start, and set the home channel again (`/sethome`) if needed.

## Verified
Verified on an isolated test gateway against a loopback Bot API stub that answers 403 with this error,
2026-10-04: one `sendMessage`, status failed, no retry.

## Related
—
