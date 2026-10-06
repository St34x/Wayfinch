<!-- SPDX-License-Identifier: Apache-2.0 -->
# Emulator `-tcpdump` capture is (almost) empty or shows no DNS names

## Symptom
The egress test finds only a handful of packets, or no DNS names/hosts at all, although the app made requests.

## Context
Emulator network capture via `emulator … -tcpdump run.pcap` for the egress test.

## Cause
(1) Traffic over the emulated **Wi-Fi** is not in the `-tcpdump` capture (only the cellular/slirp NIC is). (2) Private
DNS sends lookups over DoT to `10.0.2.3:853`, so names are invisible. (3) Capture timestamps can be off by an hour
(DST bug), so host-clock phase splits fail.

## Fix
After boot: `adb shell svc wifi disable`, `adb shell settings put global private_dns_mode off`; split phases with a
DNS marker query (`ping -c1 phase-<n>.example.com`) instead of clocks; `adb emu network capture stop` before killing
the emulator to flush the file.

## Verified
Verified on a headless Android emulator capturing with `-tcpdump`, 2026-10-04.

## Related
`android-emulator-api37-surfaceflinger-crash.md`
