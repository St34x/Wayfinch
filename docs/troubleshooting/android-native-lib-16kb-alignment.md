<!-- SPDX-License-Identifier: Apache-2.0 -->
# Native library not 16 KB page aligned

## Symptom
Play Console / Android Studio warns that native libraries are not 16 KB aligned, or `readelf -lW lib.so | grep LOAD`
shows `Align 0x1000`.

## Context
Bumping or adding a dependency that ships `.so` files (e.g. the LC3 decoder AAR).

## Cause
The library was linked for 4 KB pages; 16 KB-page devices and Play (apps targeting API 35+) need `0x4000`.

## Fix
Use an artefact built with 16 KB alignment (check every ABI with `readelf -lW jni/<abi>/*.so | grep LOAD`; a newer
release of the same artefact may be aligned when an older one is not) or rebuild from source with NDK r28+. The build
check `zipalign -c -P 16` / `readelf` must stay green.

## Verified
2026-10-04: `lc3Lib` 3.1.1 → `0x1000`; 3.3.0-dev.595 → `0x4000` on all ABIs.

## Related
—
