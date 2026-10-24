<!-- SPDX-License-Identifier: Apache-2.0 -->
# UPSTREAM — provenance of the vendored G2 driver

This folder holds a copy of the open-source driver layer for Even Realities G2 glasses, imported from the upstream
project below, pinned to one commit. It is code under another copyright holder: the upstream `LICENSE` stays next to it
unchanged, every copied file says where it came from, and every change we make is logged here.

## Upstream

| Field | Value |
|---|---|
| Repository | https://github.com/Mentra-Community/MentraOS |
| Branch of the pin | `dev` (the upstream default branch at an earlier time; not re-checked: the local checkout used for the import is detached) |
| Pinned commit | `51f16163a8ab363ede97f635fbad05bbd780c451` (2026-10-04 12:45 -0700) |
| Tag of that commit | `mentra-v3.3.0-dev.595` (not re-checked: the local checkout used for the import holds no tags) |
| Licence | Apache-2.0 (root `LICENSE` and the module `LICENSE` at the pin) |
| NOTICE | none in the upstream module at the pin; none is shipped here |
| Source of this import | a local read-only checkout of the pinned commit; nothing was fetched or cloned for the import |

## Machine-read fields

`tools/upstream-sync/verify-import.sh` reads exactly these lines and the table below. Keep them one per line.

```
upstream-commit: 51f16163a8ab363ede97f635fbad05bbd780c451
upstream-prefix: com.mentra.bluetoothsdk
our-prefix: app.hudcompanion.g2driver
author-line: The Wayfinch Authors
```

## Files

Paths in the first column are relative to this folder. `source` = a Kotlin file with two header lines and the prefix
rewrite; `verbatim` = byte-identical to the upstream blob. The sha256 is that of the **upstream blob** at the pinned
commit (what `git show <commit>:<path>` prints), not of our file.

| Our file | Upstream path | Kind | sha256 of the upstream blob |
|---|---|---|---|
| `src/main/kotlin/app/hudcompanion/g2driver/sgcs/G2.kt` | `mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt` | source | `0ee59c4e887db96f5028e68fb8ed2b61cf161c48d6d2a6ed7c56a8b3bd211216` |
| `src/main/kotlin/app/hudcompanion/g2driver/utils/G2Text.kt` | `mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/utils/G2Text.kt` | source | `5774ca6efc60574ba8ed0633a63e1b84a4beab11a8431cb2e423cb0dfe320fe6` |
| `src/main/kotlin/app/hudcompanion/g2driver/sgcs/G2SerialResolution.kt` | `mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2SerialResolution.kt` | source | `e6a18aa0a709dd7d1a78228c33f6d0653e8b2b005169fcfac1851a73acc216f2` |
| `LICENSE` | `mobile/modules/bluetooth-sdk/LICENSE` | verbatim | `f88b29454af2590b3d735acb60a9270b805b6eb49e19dc24dac0de17b070cd87` |

Not copied, on purpose: the rest of the upstream module (device manager, stores, bridge, SDK facade, analytics,
services, other devices, OTA, photo, speech, the module manifest with its permissions and foreground-service types) and
the upstream tests. The three Kotlin files reference some of those classes (`Bridge`, `DeviceStore`, `DeviceManager`,
`SGCManager`, `PhotoRequest`, `NativeNotification*`, `DeviceTypes`, `EvenFileService`, `NotificationIds`, ...); they are
replaced by our own seams in later commits, which is why the module is not compiled yet.

## Package mapping

One prefix substitution, in both directions, applied to `package` and `import` lines only:

| | Prefix |
|---|---|
| upstream | `com.mentra.bluetoothsdk` |
| ours | `app.hudcompanion.g2driver` |

Sub-packages are kept (`.sgcs`, `.utils`), so the reverse mapping restores the upstream bytes exactly. The upstream
prefix occurs nowhere else in the three files at the pin (checked at import). If the application base package changes,
the "ours" prefix, the folder names under `src/main/kotlin/` and the `our-prefix:` line change in the same commit.

## Header lines

The import adds exactly two lines at the top of each `source` file and no other text:

```
// SPDX-License-Identifier: Apache-2.0
// Copied from <upstream path>@<pinned commit>; modified by The Wayfinch Authors (see UPSTREAM.md)
```

The upstream files carry no copyright or licence header of their own at the pin (line 1 is `package ...`). If a later
sync brings one in, it stays verbatim below these two lines.

## Verifying the import

`sh tools/upstream-sync/verify-import.sh` (offline; `make check-upstream-import` runs it together with
`tools/upstream-sync/tests/test_verify_import.sh`, which plants one defect per case): per file, checks the two header
lines, fails when the upstream prefix is still anywhere in our file, reverses the package mapping, and compares the
sha256 with the table; `LICENSE` is compared as is; it also checks that the module holds exactly the listed files plus
`UPSTREAM.md` and `build.gradle.kts` (entries of any type; a symbolic link fails). `--upstream <dir>` additionally
compares each recorded sha256 with
`git -C <dir> show <commit>:<path>` of a local checkout of the upstream project (read-only; the script never fetches).
`--rev <commit>` reads our files from that commit instead of the work tree, so the import commit can still be checked
after later modifications (a modified file fails against the table by design). The first task that modifies a copied
file by design must set `UPSTREAM_IMPORT_REV` in `mk/upstream-sync.mk` to the import commit (the merge records it in
a follow-up commit, as a commit cannot hold its own hash); until then the check reads the work tree.

## Re-sync steps (mapping applied)

1. Get the new upstream state into a local checkout.
2. `git -C <checkout> diff <pinned commit>..<new commit> -- <the three upstream paths and LICENSE>`; list the hunks and
   classify each (protocol or firmware fix, display fix, audio fix, upstream UI or global plumbing to ignore, other
   device to ignore). A licence change is a stop: escalate before anything else.
3. In each hunk apply the mapping to `package`, `import` and fully-qualified names (`com.mentra.bluetoothsdk` to
   `app.hudcompanion.g2driver`), then port only the relevant hunks onto our modified files. Never copy a whole file over
   our modifications.
4. Log each ported hunk below with the upstream commit id; bump the pinned commit, the tag and the hashes in the table
   (the table keeps upstream-blob hashes); re-run the module's tests and the project gates.

## Modification log

| Commit | Change |
|---|---|
| import | Files copied from the pinned commit; two header lines added per Kotlin file; `package`/`import` prefix rewritten by the mapping; `LICENSE` copied unchanged. No other change. |
