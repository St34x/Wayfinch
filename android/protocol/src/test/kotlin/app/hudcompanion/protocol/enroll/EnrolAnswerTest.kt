// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assertions.fail
import org.junit.jupiter.api.Test
import java.util.Base64

/**
 * SAT-PL-35 (T1.23c): a `200` is success only with exactly `cert` (one PEM certificate),
 * `token` and `deviceId`; every other answer is a failure state without a retry hint, and a non-`200` body is never
 * read, parsed or logged.
 */
class EnrolAnswerTest {
    /** A body that counts its reads; [MARKER] in its token shows up wherever its text leaks. */
    private class Body(
        text: String,
    ) : EnrolAnswerBody {
        val bytes = text.toByteArray(Charsets.UTF_8)
        var reads = 0

        override fun read(limit: Int): ByteArray {
            reads += 1
            return bytes.copyOf(minOf(limit, bytes.size))
        }
    }

    private val log = mutableListOf<String>()

    private fun answer(
        status: Int,
        body: EnrolAnswerBody,
    ): EnrolAnswer = EnrolAnswer.of(status, body) { log.add(it) }

    private fun answer200(text: String): EnrolAnswer = answer(200, Body(text))

    private fun failed200(
        text: String,
        case: String = text,
    ) = assertEquals(EnrolAnswer.Failed(EnrolFailure.BAD_ANSWER), answer200(text), case)

    @Test
    fun `a 200 with exactly cert, token and deviceId is issued`() {
        val issued = answer200(json()) as? EnrolAnswer.Issued ?: fail("not issued")
        assertEquals(CERT, issued.certPem)
        assertEquals(TOKEN, issued.token)
        assertEquals(DEVICE_ID, issued.deviceId)
        assertEquals(Base64.getEncoder().encodeToString(DER), PEM_BODY.replace("\n", ""))
        listOf(json(cert = CERT.removeSuffix("\n")), json(token = "a"), json(token = "A".repeat(256))).forEach {
            assertTrue(answer200(it) is EnrolAnswer.Issued, it)
        }
    }

    @Test
    fun `a 200 with a fourth key, a missing key or a non-string value fails`() {
        failed200(json(extra = "retryAfter"), "fourth key")
        failed200(json(extra = "sig"), "fourth key")
        failed200("""{"token":"$TOKEN","deviceId":"$DEVICE_ID"}""", "no cert")
        failed200(json().replace("\"token\"", "\"Token\""), "renamed key")
        failed200("""{"cert":null,"token":"$TOKEN","deviceId":"$DEVICE_ID"}""", "null cert")
        failed200(json().replace("\"$DEVICE_ID\"", "81985529216486895"), "number deviceId")
        failed200(json().replace("\"$TOKEN\"", "[\"$TOKEN\"]"), "array token")
    }

    @Test
    fun `a 200 whose cert is not exactly one PEM certificate block fails`() {
        val block = "-----BEGIN CERTIFICATE-----\n$PEM_BODY-----END CERTIFICATE-----\n"
        val certs =
            listOf(
                "",
                PEM_BODY,
                block + block,
                "x$block",
                block + "x",
                block.replace("CERTIFICATE", "PRIVATE KEY"),
                "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n",
                block.replace("\n", "\r\n"),
                block.replaceFirst(PEM_BODY.take(4), "@@@@"),
                block.replaceFirst(PEM_BODY.take(4), "-AB_"),
                "-----BEGIN CERTIFICATE-----\nAAA=\nAAAA\n-----END CERTIFICATE-----\n",
                "-----BEGIN CERTIFICATE-----\nAAB=\n-----END CERTIFICATE-----\n",
                "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n",
            )
        certs.forEach { failed200(json(cert = it)) }
    }

    @Test
    fun `a 200 with a bad token or deviceId fails`() {
        val tokens = listOf("", "A".repeat(257), "a b", "a+b", "a/b", "a=", "\u00E4", "a\n")
        tokens.forEach { failed200(json(token = it)) }
        val ids = listOf("", "0123456789abcde", "0123456789abcdef0", "0123456789ABCDEF", "0123456789abcdeg")
        (ids + " 123456789abcdef").forEach { failed200(json(deviceId = it)) }
    }

    @Test
    fun `a 200 with an escaped lone surrogate in the token or deviceId fails`() {
        // the reader leaves lone surrogates to its callers (see the StrictJson KDoc): the field shapes must refuse them
        val token = json().replace("\"$TOKEN\"", "\"a\\uD800\"")
        val deviceId = json().replace("\"$DEVICE_ID\"", "\"0123456789abcde\\uDC00\"")
        listOf(token, deviceId).forEach {
            assertTrue("\\uD800" in it || "\\uDC00" in it, "the escape is in the body")
            failed200(it)
        }
    }

    @Test
    fun `a 200 body that is not strict JSON, not UTF-8 or over 8192 bytes fails`() {
        failed200(json().replaceFirst("{", "{\"token\":\"x\","), "repeated key")
        failed200(json() + " x", "trailing bytes")
        failed200("\uFEFF" + json(), "BOM")
        failed200("[" + json() + "]", "not an object")
        val exact = json().padEnd(EnrolAnswer.MAX_BODY_BYTES, ' ')
        assertTrue(answer200(exact) is EnrolAnswer.Issued, "8192 bytes")
        failed200("$exact ", "8193 bytes")
        val badUtf8 = json(token = "Abc").toByteArray(Charsets.UTF_8)
        badUtf8[json(token = "Abc").indexOf("Abc")] = 0xC3.toByte()
        assertEquals(EnrolAnswer.Failed(EnrolFailure.BAD_ANSWER), answer(200, RawBody(badUtf8)))
    }

    private class RawBody(
        private val bytes: ByteArray,
    ) : EnrolAnswerBody {
        override fun read(limit: Int): ByteArray = bytes.copyOf(minOf(limit, bytes.size))
    }

    @Test
    fun `every other status is a failure state and its body is never read, parsed or logged`() {
        val named =
            mapOf(
                400 to EnrolFailure.BAD_REQUEST,
                404 to EnrolFailure.REFUSED,
                413 to EnrolFailure.TOO_LARGE,
                429 to EnrolFailure.RATE_LIMITED,
                503 to EnrolFailure.UNAVAILABLE,
            )
        val other = listOf(-1, 0, 100, 199, 201, 204, 301, 304, 401, 403, 405, 500, 502, 504, 999)
        (named + other.associateWith { EnrolFailure.UNEXPECTED_STATUS }).forEach { (status, kind) ->
            val body = Body(json(token = MARKER))
            log.clear()
            assertEquals(EnrolAnswer.Failed(kind), answer(status, body), "$status")
            assertEquals(0, body.reads, "body of $status read")
            assertEquals(listOf("enrol_answer status=$status outcome=$kind"), log)
        }
    }

    @Test
    fun `a 404 is the refused state of the ask-the-operator message`() {
        assertEquals(EnrolAnswer.Failed(EnrolFailure.REFUSED), answer(404, Body("not found")))
    }

    @Test
    fun `a failure carries only its kind, never a retry hint`() {
        val fields =
            EnrolAnswer.Failed::class.java.declaredFields
                .filter { !it.isSynthetic }
                .map { it.name }
        assertEquals(listOf("kind"), fields)
        assertFalse(EnrolFailure.entries.any { it.name.contains("RETRY") })
    }

    @Test
    fun `the log gets one fixed code per answer, never the token or the body`() {
        answer200(json(token = MARKER))
        failed200(json(token = MARKER, extra = "x"))
        val expected = listOf("enrol_answer status=200 outcome=ISSUED", "enrol_answer status=200 outcome=BAD_ANSWER")
        assertEquals(expected, log)
        val issued = answer200(json(token = MARKER))
        assertFalse(issued.toString().contains(MARKER), issued.toString())
    }

    private companion object {
        const val TOKEN = "tok_Abc-123"
        const val DEVICE_ID = "0123456789abcdef"
        const val MARKER = "BodyMarker_7f3a"
        val DER = ByteArray(300) { (it * 13 + 5).toByte() }
        val PEM_BODY =
            Base64
                .getEncoder()
                .encodeToString(DER)
                .chunked(64)
                .joinToString("") { it + "\n" }
        val CERT = "-----BEGIN CERTIFICATE-----\n$PEM_BODY-----END CERTIFICATE-----\n"

        fun json(
            cert: String = CERT,
            token: String = TOKEN,
            deviceId: String = DEVICE_ID,
            extra: String? = null,
        ): String =
            buildJsonObject {
                put("cert", cert)
                put("token", token)
                put("deviceId", deviceId)
                if (extra != null) put(extra, JsonPrimitive("1"))
            }.toString()
    }
}
