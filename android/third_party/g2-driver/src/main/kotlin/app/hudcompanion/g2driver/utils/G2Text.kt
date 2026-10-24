// SPDX-License-Identifier: Apache-2.0
// Copied from mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/utils/G2Text.kt@51f16163a8ab363ede97f635fbad05bbd780c451; modified by The Wayfinch Authors (see UPSTREAM.md)
package app.hudcompanion.g2driver.utils

/** Encode blank rows without triggering the EvenHub empty-line parser bug. */
internal object G2Text {
  fun containerContent(text: String): String =
    if (text.isEmpty()) " " else text.split("\n").joinToString("\n") { it.ifEmpty { "\u200B" } }
}
