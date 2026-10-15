<!-- SPDX-License-Identifier: Apache-2.0 -->
# Voice commands arrive translated into English

## Symptom
Non-English voice commands reach the agent as English text, e.g. a Russian or French command is answered as if it
had been spoken in English. No error is logged.

## Context
On the fallback voice path (no streaming speech-to-text configured or available), voice clips from the glasses are
VOICE events transcribed by whatever `stt.provider` the Hermes install uses.

## Cause
Hermes' default `stt.language` is `"en"`; with Whisper-family providers a fixed `en` means *translate to English*.

## Fix
Set the language to auto-detect in the Hermes config: top-level `stt.language: ""` (and/or
`stt.<provider>.language: ""`), then restart the gateway.

## Verified
Verified on Hermes `a4648c5f58` with faster-whisper, 2026-10-04: non-English clips were translated with `en` and
transcribed verbatim with `""`.

## Related
`faster-whisper-pyav-metadata-errors.md`
