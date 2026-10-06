<!-- SPDX-License-Identifier: Apache-2.0 -->
# API 37 x86_64 emulator crash-loops headless

## Symptom
The API 37 `google_apis` x86_64 emulator boots (`sdk=37`) but the UI restarts every ~10 s; tombstone:
`Abort message: 'Assertion failed: !rcEnc->featureInfo()->hasReadColorBufferDma'` in `/system/bin/surfaceflinger`.

## Context
Headless emulator (`-no-window`) with software GL, emulator 37.2.12, images 37.0 r6 / 37.1 ps16k r9.

## Cause
A graphics-feature assertion in the image's surfaceflinger with the software renderer.

## Fix
No `-gpu` mode or `-feature -GLDMA` flag helped. Run runtime checks on an **API 36** image (with a test-only minSdk
override) and API-37-only checks on a real device.

## Verified
Verified on a headless emulator with the versions above, 2026-10-04.

## Related
`android-emulator-egress-capture-empty.md`
