<!-- SPDX-License-Identifier: Apache-2.0 -->
# Local faster-whisper STT fails on every clip

## Symptom
Every voice clip fails to transcribe with
`open() got an unexpected keyword argument 'metadata_errors'`.

## Context
Hermes local STT with faster-whisper 1.2.1 in a venv where `av` (PyAV) was installed unpinned.

## Cause
PyAV 19 changed `av.open()`; faster-whisper 1.2.1 still passes `metadata_errors`.

## Fix
Pin `av==18.0.0` next to `faster-whisper==1.2.1` in the environment that runs STT, reinstall, restart the gateway.

## Verified
Verified on a Hermes install with local faster-whisper STT, 2026-10-04: PyAV 19 → every clip failed; `av==18.0.0` →
clips transcribed.

## Related
`hermes-stt-language-en-translates.md`
