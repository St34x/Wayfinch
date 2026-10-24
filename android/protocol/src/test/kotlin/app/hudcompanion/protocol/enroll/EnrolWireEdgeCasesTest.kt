// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable
import java.io.IOException
import java.util.Base64

/** Size limits, odd bytes and malformed answers of the request body, the proof bytes and the `/enroll` answer. */
class EnrolWireEdgeCasesTest {
    private val signature = ByteArray(SIGNATURE_BYTES) { (it * 7 + 1).toByte() }
    private val log = mutableListOf<String>()

    private fun request(
        spki: ByteArray = EnrolVectors.spki(),
        code: String = Payloads.CODE,
        signature: ByteArray = this.signature,
    ): EnrolRequest = EnrolRequest.of(spki, code, signature)

    private fun answer(
        status: Int,
        read: (Int) -> ByteArray,
    ): EnrolAnswer = EnrolAnswer.of(status, { read(it) }) { log.add(it) }

    private fun answer200(text: String): EnrolAnswer = answer(OK) { text.toByteArray().copyOf(minOf(it, text.length)) }

    private val issued = "{\"cert\":${quote(CERT)},\"token\":\"tok_Abc-123\",\"deviceId\":\"0123456789abcdef\"}"

    @Test
    fun `the largest spki and signature and the smallest ones are accepted, one more or less is refused`() {
        assertTrue(request(spki = ByteArray(1)) is EnrolRequest.Ready)
        assertTrue(request(spki = ByteArray(EnrolRequest.MAX_SPKI_BYTES)) is EnrolRequest.Ready)
        assertTrue(request(signature = ByteArray(1)) is EnrolRequest.Ready)
        assertTrue(request(signature = ByteArray(EnrolRequest.MAX_SIGNATURE_BYTES)) is EnrolRequest.Ready)
        val bad = EnrolRequestRefusal.BAD_SPKI
        assertEquals(EnrolRequest.Refused(bad), request(spki = ByteArray(0)))
        assertEquals(EnrolRequest.Refused(bad), request(spki = ByteArray(EnrolRequest.MAX_SPKI_BYTES + 1)))
        assertEquals(EnrolRequest.Refused(bad), request(spki = ByteArray(HUGE)))
        val sig = EnrolRequestRefusal.BAD_SIGNATURE
        assertEquals(EnrolRequest.Refused(sig), request(signature = ByteArray(0)))
        assertEquals(EnrolRequest.Refused(sig), request(signature = ByteArray(EnrolRequest.MAX_SIGNATURE_BYTES + 1)))
        assertEquals(EnrolRequest.Refused(sig), request(signature = ByteArray(HUGE)))
    }

    @Test
    fun `a code that is nearly right is refused`() {
        val near = Payloads.CODE
        val codes =
            listOf(
                near + "\n",
                "\n" + near,
                " $near",
                "$near ",
                near.dropLast(1) + "\u00E9",
                near.dropLast(1) + "\u0000",
                "",
            )
        assertAll(
            codes.map { code ->
                Executable {
                    assertEquals(
                        EnrolRequest.Refused(EnrolRequestRefusal.BAD_CODE),
                        request(code = code),
                        code,
                    )
                }
            },
        )
    }

    @Test
    fun `base64 plus and slash characters and the largest body are written as they are on one line`() {
        val plus = ByteArray(EnrolRequest.MAX_SPKI_BYTES) { PLUS_PATTERN[it % PLUS_PATTERN.size] }
        val slash = ByteArray(EnrolRequest.MAX_SIGNATURE_BYTES) { 0xFF.toByte() }
        val ready = request(spki = plus, signature = slash) as EnrolRequest.Ready
        assertTrue(ready.body.contains("++++") && ready.body.contains("////"), ready.body)
        assertFalse(ready.body.any { it == '\n' || it == '\r' || it == '\\' }, ready.body)
        assertTrue(ready.body.toByteArray().size <= EnrolRequest.MAX_BODY_BYTES)
        val fields = ready.body.split('"')
        assertArrayEquals(plus, Base64.getDecoder().decode(fields[SPKI_AT]))
        assertArrayEquals(slash, Base64.getDecoder().decode(fields[SIGNATURE_AT]))
        assertEquals(keyFingerprint(plus), ready.fingerprint)
    }

    @Test
    fun `the fingerprint of an empty key and the proof bytes of an empty key are defined`() {
        assertEquals("E3B0 C442 98FC 1C14", keyFingerprint(ByteArray(0)))
        assertEquals(PROOF_PREFIX_BYTES + Payloads.CODE.length, EnrolVectors.proof(Payloads.CODE, ByteArray(0)).size)
        val large = ByteArray(EnrolRequest.MAX_SPKI_BYTES) { it.toByte() }
        val size = PROOF_PREFIX_BYTES + CODE_BYTES + large.size
        assertArrayEquals(
            large,
            EnrolVectors.proof(Payloads.CODE, large).copyOfRange(PROOF_PREFIX_BYTES + CODE_BYTES, size),
        )
    }

    @Test
    fun `a code that is nearly right is no proof input`() {
        val near = Payloads.CODE
        listOf(near + "\n", " $near", near.dropLast(1) + "\u00E9", near.dropLast(1) + "\u0000", "").forEach {
            assertNull(proofBytes(it, EnrolVectors.spki()), it)
        }
    }

    @Test
    fun `every strict prefix of a valid 200 body fails and the whole body is issued`() {
        assertTrue(answer200(issued) is EnrolAnswer.Issued)
        val failed = EnrolAnswer.Failed(EnrolFailure.BAD_ANSWER)
        assertAll(
            (0 until issued.length).map { cut ->
                Executable { assertEquals(failed, answer200(issued.substring(0, cut)), "cut at $cut") }
            },
        )
    }

    @Test
    fun `a 200 with an empty body, a body past the bound or a body that ignores the limit fails`() {
        val failed = EnrolAnswer.Failed(EnrolFailure.BAD_ANSWER)
        assertEquals(failed, answer(OK) { ByteArray(0) })
        assertEquals(failed, answer(OK) { ByteArray(HUGE) { ' '.code.toByte() } })
        assertEquals(failed, answer200(" ".repeat(EnrolAnswer.MAX_BODY_BYTES + 1) + issued))
        assertEquals(failed, answer200("{\"cert\":".repeat(DEEP) + "1" + "}".repeat(DEEP)))
        assertEquals(failed, answer200("[".repeat(DEEP_ARRAYS)))
    }

    @Test
    fun `the body is read once with a limit one past the bound and nothing else is logged`() {
        val limits = mutableListOf<Int>()
        val result = answer(OK) { limit -> limits.add(limit).let { issued.toByteArray() } }
        assertTrue(result is EnrolAnswer.Issued)
        assertEquals(listOf(EnrolAnswer.MAX_BODY_BYTES + 1), limits)
        assertEquals(listOf("enrol_answer status=200 outcome=ISSUED"), log)
    }

    @Test
    fun `a repeated answer key is refused also when it is spelled with an escape`() {
        val failed = EnrolAnswer.Failed(EnrolFailure.BAD_ANSWER)
        assertEquals(failed, answer200(issued.dropLast(1) + ",\"c\\u0065rt\":\"x\"}"))
        assertEquals(failed, answer200(issued.dropLast(1) + ",\"token\":\"other\"}"))
    }

    @Test
    fun `a pretty printed answer is read, a cert with stray whitespace or case is not`() {
        val pretty = "{\n  \"cert\": ${quote(
            CERT,
        )},\n  \"token\": \"tok_Abc-123\",\n  \"deviceId\": \"0123456789abcdef\"\n}\n"
        assertTrue(answer200(pretty) is EnrolAnswer.Issued)
        val failed = EnrolAnswer.Failed(EnrolFailure.BAD_ANSWER)
        val certs =
            listOf(
                "\n$CERT",
                " $CERT",
                "$CERT\n",
                CERT.replace("BEGIN CERTIFICATE", "begin certificate"),
                CERT.replace("AAAA", "AA AA"),
                CERT.replace("\n", "\n\n"),
            )
        assertAll(
            certs.map { cert ->
                Executable { assertEquals(failed, answer200(issued.replace(quote(CERT), quote(cert))), cert) }
            },
        )
    }

    @Test
    fun `extreme statuses are unexpected with their body unread, a failing 200 body read reaches the caller`() {
        listOf(Int.MIN_VALUE, Int.MAX_VALUE, 0, -200).forEach { status ->
            var reads = 0
            val result =
                answer(status) {
                    reads += 1
                    ByteArray(0)
                }
            assertEquals(EnrolAnswer.Failed(EnrolFailure.UNEXPECTED_STATUS), result, "$status")
            assertEquals(0, reads)
        }
        log.clear()
        assertThrows(IOException::class.java) { answer(OK) { throw IOException("connection reset") } }
        assertTrue(log.isEmpty(), log.toString())
    }

    private companion object {
        const val OK = 200
        const val HUGE = 1 shl 20
        const val DEEP = 500
        const val DEEP_ARRAYS = 8_000
        const val SIGNATURE_BYTES = 71
        const val PROOF_PREFIX_BYTES = 19
        const val CODE_BYTES = 22
        const val SPKI_AT = 3
        const val SIGNATURE_AT = 11
        val PLUS_PATTERN = byteArrayOf(0xFB.toByte(), 0xEF.toByte(), 0xBE.toByte())
        const val CERT = "-----BEGIN CERTIFICATE-----\nAAAAAAAA\nAAAAAAAA\n-----END CERTIFICATE-----\n"

        fun quote(text: String): String = "\"" + text.replace("\n", "\\n") + "\""
    }
}
