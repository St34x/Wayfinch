// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import java.util.Base64

/** Builders of valid and broken enrolment payloads for the T1.23a tests. */
internal object Payloads {
    const val CODE = "AbCdEfGhIjKlMnOpQrSt_-"
    const val ORIGIN = "https://glasses.example.com"

    fun pin(fill: Int): String = "sha256/" + Base64.getEncoder().encodeToString(ByteArray(32) { fill.toByte() })

    val pins = listOf(pin(1), pin(2))

    fun json(
        version: String = "1",
        url: String = "\"$ORIGIN\"",
        code: String = "\"$CODE\"",
        pins: String = Payloads.pins.joinToString(",", "[", "]") { "\"$it\"" },
    ): String = """{"version":$version,"url":$url,"code":$code,"pins":$pins}"""

    fun withUrl(url: String): String = json(url = "\"$url\"")

    fun link(json: String): String = "wayfinch://enroll#" + fragment(json.toByteArray())

    fun fragment(bytes: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)

    /** A link whose fragment keeps its `=` padding (the JSON is padded with spaces so that padding exists). */
    fun paddedLink(): String {
        var text = json()
        while (text.length % 3 == 0) text += " "
        return "wayfinch://enroll#" + Base64.getUrlEncoder().encodeToString(text.toByteArray())
    }
}
