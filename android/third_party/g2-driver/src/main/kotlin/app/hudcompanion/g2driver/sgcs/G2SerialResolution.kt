// SPDX-License-Identifier: Apache-2.0
// Copied from mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2SerialResolution.kt@51f16163a8ab363ede97f635fbad05bbd780c451; modified by The Wayfinch Authors (see UPSTREAM.md)
package app.hudcompanion.g2driver.sgcs

/**
 * Which manufacturing serial to publish for a G2 connection.
 *
 * A fresh pairing decodes the serial from the advertisement during the scan. A
 * cached reconnect (address based, after a process restart) skips the scan, so the
 * only serial the SDK still holds is the one it persisted as the device name at
 * the previous auth completion. That persisted value is reused only when it is
 * exactly the id this connection was asked for: the search id can be a partial
 * string typed by a host, and a partial must never be reported as a serial.
 */
internal object G2SerialResolution {
    private const val UNSET = "NOT_SET"

    fun resolve(
        scannedSerial: String?,
        requestedId: String,
        persistedDeviceName: String,
    ): String? =
        scannedSerial?.trim()?.takeIf { it.isNotEmpty() }
            ?: persistedDeviceName.trim().takeIf { it.isNotEmpty() && it != UNSET && it == requestedId.trim() }
}
