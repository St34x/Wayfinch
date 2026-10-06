<!-- SPDX-License-Identifier: Apache-2.0 -->
# `adb devices` shows `no permissions` for a user logged in over SSH

## Symptom
`adb devices` prints `<serial> no permissions (user <name> is not in the plugdev group)` although a udev rule with
`TAG+="uaccess"` exists.

## Context
Building/installing the app from a Linux machine where the build user logs in over SSH (no desktop seat).

## Cause
`uaccess` grants the USB device to the user of the **active seat** (the desktop session), not to SSH users.

## Fix
Use a group rule instead: `SUBSYSTEM=="usb", ATTR{idVendor}=="<vendor>", MODE="0660", GROUP="plugdev"`, add the
build user to `plugdev`, `udevadm control --reload-rules && udevadm trigger`, re-plug the phone, **log in again**
(groups update on login), `adb kill-server; adb devices`, accept the RSA prompt on the phone.

## Verified
Verified on a Linux build host whose build user logs in over SSH, 2026-10-04 (sd-login(3): uaccess is seat-bound;
Android "Run apps on a hardware device" page).

## Related
—
