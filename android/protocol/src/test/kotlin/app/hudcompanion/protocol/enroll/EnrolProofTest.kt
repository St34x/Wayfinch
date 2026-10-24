// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import app.hudcompanion.protocol.enroll.EnrolVectors.hex
import app.hudcompanion.protocol.enroll.EnrolVectors.sha256Hex
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * SAT-PL-32 (proof bytes) and SAT-PL-34 (the key fingerprint formatter); T1.23c.
 */
class EnrolProofTest {
    @Test
    fun `proof bytes are the prefix with its NUL byte, the code as ASCII, then the SPKI DER (golden vector)`() {
        val prefix = "77617966696e63682d656e726f6c6c2d763100"
        val code = "4162436445664768496a4b6c4d6e4f70517253745f2d"
        val spki =
            "3059301306072a8648ce3d020106082a8648ce3d030107034200046b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4" +
                "a13945d898c2964fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5"
        assertEquals(prefix + code + spki, hex(EnrolVectors.proof(Payloads.CODE, EnrolVectors.spki())))
    }

    @Test
    fun `the prefix is 18 ASCII bytes and one NUL byte, the SPKI is appended unchanged`() {
        val proof = EnrolVectors.proof(Payloads.CODE, byteArrayOf(0x30, 0x00, -1))
        assertEquals(PREFIX_BYTES + Payloads.CODE.length + 3, proof.size)
        assertEquals("wayfinch-enroll-v1", String(proof, 0, PREFIX_BYTES - 1, Charsets.US_ASCII))
        assertEquals(0, proof[PREFIX_BYTES - 1].toInt())
        assertEquals("3000ff", hex(proof.copyOfRange(proof.size - 3, proof.size)))
    }

    @Test
    fun `a code that is not 22 base64url characters has no proof bytes`() {
        val codes = listOf("", "short", Payloads.CODE + "A", "AbCdEfGhIjKlMnOpQrSt_\u00E9", "AbCdEfGhIjKlMnOpQrSt+/")
        codes.forEach { code -> assertNull(proofBytes(code, EnrolVectors.spki()), code) }
    }

    @Test
    fun `the shared vector SPKI displays as 5CD2 52FB 0CE8 9324`() {
        assertEquals(EnrolVectors.SPKI_SHA256, sha256Hex(EnrolVectors.spki()))
        assertEquals(EnrolVectors.FINGERPRINT, keyFingerprint(EnrolVectors.spki()))
    }

    @Test
    fun `the fingerprint is the first 16 hex digits of SHA-256, upper case, four groups of four`() {
        val inputs = listOf(byteArrayOf(), byteArrayOf(1), ByteArray(256) { it.toByte() }, EnrolVectors.spki())
        inputs.forEach { bytes ->
            val expected =
                sha256Hex(bytes)
                    .take(16)
                    .uppercase()
                    .chunked(4)
                    .joinToString(" ")
            val shown = keyFingerprint(bytes)
            assertEquals(expected, shown)
            assertTrue(Regex("[0-9A-F]{4}( [0-9A-F]{4}){3}").matches(shown), shown)
        }
    }

    private companion object {
        const val PREFIX_BYTES = 19
    }
}
