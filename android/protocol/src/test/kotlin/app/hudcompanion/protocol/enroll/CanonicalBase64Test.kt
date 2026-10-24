// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable

/** The one canonical-base64 check behind the server pins and the PEM body. */
class CanonicalBase64Test {
    @Test
    fun `canonical padded base64 of one or more bytes gives the bytes`() {
        assertArrayEquals(byteArrayOf(0x4d), canonicalBase64OrNull("TQ=="))
        assertArrayEquals(byteArrayOf(0x4d, 0x61), canonicalBase64OrNull("TWE="))
        assertArrayEquals("Man".toByteArray(), canonicalBase64OrNull("TWFu"))
        assertArrayEquals(byteArrayOf(-5, -1), canonicalBase64OrNull("+/8="))
    }

    @Test
    fun `empty, unpadded, over-padded, non-zero pad bits, URL alphabet, spaces and line breaks give null`() {
        val texts =
            listOf(
                "", // no bytes
                "TQ", // padding missing
                "TQ=", // padding short
                "TQ===", // padding surplus
                "TR==", // non-zero pad bits
                "TWF=", // non-zero pad bits
                "-_8=", // URL alphabet
                "TQ ==",
                "TWFu\n",
                "TW\nFu",
                "TQ==TQ==", // padding in the middle
                "T\u00C9==",
                "=",
            )
        assertAll(texts.map { text -> Executable { assertNull(canonicalBase64OrNull(text), text) } })
    }
}
