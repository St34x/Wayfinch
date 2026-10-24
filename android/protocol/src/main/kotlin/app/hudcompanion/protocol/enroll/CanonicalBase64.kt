// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import java.util.Base64

/**
 * The bytes of [text] when it is canonical padded standard base64 of one or more bytes, else null: the decoder must
 * accept it (no line breaks, no URL alphabet, no `=` in the middle) and re-encoding the bytes must give [text] again
 * (so non-zero pad bits and a missing or surplus `=` are refused). The one check behind the server pins and the PEM
 * body of the `/enroll` answer.
 */
internal fun canonicalBase64OrNull(text: String): ByteArray? {
    val bytes =
        try {
            Base64.getDecoder().decode(text)
        } catch (expected: IllegalArgumentException) {
            null
        }
    return bytes?.takeIf { it.isNotEmpty() && Base64.getEncoder().encodeToString(it) == text }
}
