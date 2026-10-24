// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import app.hudcompanion.protocol.json.StrictJson
import app.hudcompanion.protocol.json.StrictJsonResult
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assertions.fail
import org.junit.jupiter.api.Test
import java.util.Base64

/**
 * SAT-PL-33 (request body), SAT-PL-34 (the fingerprint comes from the bytes that fill `spki`; SAT-SC-14) and T1.23c
 * (strict base64 round trip).
 */
class EnrolRequestTest {
    private val signature = ByteArray(71) { (it * 7 + 1).toByte() }

    private fun ready(
        spki: ByteArray = EnrolVectors.spki(),
        code: String = Payloads.CODE,
        signature: ByteArray = this.signature,
    ): EnrolRequest.Ready =
        EnrolRequest.of(spki, code, signature) as? EnrolRequest.Ready ?: fail("refused: ${spki.size} ${signature.size}")

    private fun fields(body: String): JsonObject =
        when (val read = StrictJson.readObject(body, EnrolRequest.MAX_BODY_BYTES, 1)) {
            is StrictJsonResult.Read -> read.value
            is StrictJsonResult.Refused -> fail("not strict JSON: ${read.reason}")
        }

    private fun text(
        body: String,
        key: String,
    ): String {
        val value = fields(body)[key] as? JsonPrimitive
        assertTrue(value?.isString == true, key)
        return value?.content.orEmpty()
    }

    @Test
    fun `the body is exactly spki, code and signature, the binary values as canonical padded base64`() {
        val signatureText = Base64.getEncoder().encodeToString(signature)
        val expected =
            """{"spki":"${EnrolVectors.SPKI_BASE64}","code":"${Payloads.CODE}","signature":"$signatureText"}"""
        assertEquals(expected, ready().body)
        assertTrue(signatureText.endsWith("=") && !signatureText.endsWith("=="), signatureText)
        assertTrue(EnrolVectors.SPKI_BASE64.endsWith("=="))
    }

    @Test
    fun `the body parses as strict JSON with exactly three string values and never names sig`() {
        val body = ready().body
        assertEquals(setOf("spki", "code", "signature"), fields(body).keys)
        listOf("spki", "code", "signature").forEach { text(body, it) }
        assertFalse(body.contains("\"sig\""), body)
        assertEquals(Payloads.CODE, text(body, "code"))
    }

    @Test
    fun `spki and signature round-trip through a strict base64 decoder and re-encode to the same text`() {
        val sizes = listOf(1, 2, 3, 4, 70, 71, 72, 80)
        sizes.forEach { size ->
            val spki = ByteArray(size * 3) { (it + size).toByte() }
            val signature = ByteArray(size) { (it * 31 - size).toByte() }
            val body = ready(spki = spki, signature = signature).body
            assertRoundTrip(spki, text(body, "spki"))
            assertRoundTrip(signature, text(body, "signature"))
        }
    }

    private fun assertRoundTrip(
        bytes: ByteArray,
        encoded: String,
    ) {
        assertTrue(Regex("[A-Za-z0-9+/]*={0,2}").matches(encoded) && encoded.length % 4 == 0, encoded)
        val decoded = Base64.getDecoder().decode(encoded)
        assertArrayEquals(bytes, decoded)
        assertEquals(encoded, Base64.getEncoder().encodeToString(decoded))
    }

    @Test
    fun `the largest accepted body stays within 4096 bytes`() {
        val body = ready(spki = ByteArray(256) { -1 }, signature = ByteArray(80) { -1 }).body
        assertTrue(body.toByteArray(Charsets.UTF_8).size <= EnrolRequest.MAX_BODY_BYTES, "${body.length}")
    }

    @Test
    fun `an spki or signature out of range or a bad code is refused`() {
        val spki = EnrolVectors.spki()
        val cases =
            listOf(
                EnrolRequest.of(ByteArray(0), Payloads.CODE, signature) to EnrolRequestRefusal.BAD_SPKI,
                EnrolRequest.of(ByteArray(257), Payloads.CODE, signature) to EnrolRequestRefusal.BAD_SPKI,
                EnrolRequest.of(spki, Payloads.CODE, ByteArray(0)) to EnrolRequestRefusal.BAD_SIGNATURE,
                EnrolRequest.of(spki, Payloads.CODE, ByteArray(81)) to EnrolRequestRefusal.BAD_SIGNATURE,
                EnrolRequest.of(spki, "short", signature) to EnrolRequestRefusal.BAD_CODE,
                EnrolRequest.of(spki, Payloads.CODE + "\"", signature) to EnrolRequestRefusal.BAD_CODE,
            )
        cases.forEach { (result, reason) -> assertEquals(EnrolRequest.Refused(reason), result) }
    }

    @Test
    fun `the fingerprint shown is computed over the same bytes that fill spki`() {
        val request = ready()
        assertEquals(EnrolVectors.FINGERPRINT, request.fingerprint)
        assertEquals(keyFingerprint(Base64.getDecoder().decode(text(request.body, "spki"))), request.fingerprint)
    }

    @Test
    fun `the fingerprint is over the exact bytes sent, also where a re-encoding of the key would differ`() {
        val longForm = Base64.getDecoder().decode(EnrolVectors.SPKI_LONG_FORM_BASE64)
        val request = ready(spki = longForm)
        assertEquals(EnrolVectors.SPKI_LONG_FORM_BASE64, text(request.body, "spki"))
        assertEquals(EnrolVectors.LONG_FORM_FINGERPRINT, request.fingerprint)
        val opaque = byteArrayOf(1, 2, 3)
        assertEquals(keyFingerprint(opaque), ready(spki = opaque).fingerprint)
    }

    @Test
    fun `the body and fingerprint are built eagerly, so later changes to the caller's arrays change neither`() {
        val spki = EnrolVectors.spki()
        val signature = this.signature.copyOf()
        val request = ready(spki = spki, signature = signature)
        spki.fill(0)
        signature.fill(0)
        assertEquals(EnrolVectors.SPKI_BASE64, text(request.body, "spki"))
        assertEquals(Base64.getEncoder().encodeToString(this.signature), text(request.body, "signature"))
        assertEquals(EnrolVectors.FINGERPRINT, request.fingerprint)
    }

    @Test
    fun `the text form leaves the one-time code and the body out`() {
        val shown = ready().toString()
        assertFalse(shown.contains(Payloads.CODE), shown)
        assertTrue(shown.contains(EnrolVectors.FINGERPRINT), shown)
    }
}
