// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.revoke

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import java.io.ByteArrayInputStream

/**
 * SAT-PL-70 (T1.24d): marked only for status `403` + `Content-Type` `application/json`
 * (parameter `charset=utf-8` allowed) + body ≤1024 bytes parsing to one JSON object whose `error` is exactly
 * `device_revoked`; unmarked: `text/plain`, other JSON `error`, `Device_Revoked`, HTML, empty, 1025 bytes, a JSON
 * array, trailing garbage, a duplicate `error` key. Close `4403` is a signal whatever its reason text.
 */
class RevocationMarkerTest {
    private fun upgrade(
        contentType: String?,
        body: String,
        status: Int = 403,
    ): RevocationSignal =
        RevocationMarker.classifyUpgrade(
            status,
            contentType,
            ByteArrayInputStream(body.toByteArray(Charsets.UTF_8)),
        )

    /** A marked body padded with spaces inside the object to exactly [size] UTF-8 bytes. */
    private fun paddedMarker(size: Int): String {
        val pad = size - MARKED.length
        return "{" + " ".repeat(pad) + MARKED.substring(1)
    }

    /** The marker plus a second member whose string value holds the raw [bytes]. */
    private fun withBytesInValue(vararg bytes: Int): ByteArray =
        "{\"error\":\"device_revoked\",\"x\":\"".toByteArray() +
            ByteArray(bytes.size) { bytes[it].toByte() } +
            "\"}".toByteArray()

    @Test
    fun `the exact marker with the JSON media type is revoked`() {
        for (type in MARKED_TYPES) {
            assertEquals(RevocationSignal.Revoked, upgrade(type, MARKED), type)
        }
        assertEquals(RevocationSignal.Revoked, upgrade(JSON, " {\"error\" : \"device_revoked\"}\n"))
        assertEquals(RevocationSignal.Revoked, upgrade(JSON, "{\"error\":\"device_revoked\",\"x\":1}"))
        val valid = withBytesInValue(0xC3, 0xA9) // the same shape as the invalid rows, with valid UTF-8 (e-acute)
        assertEquals(RevocationSignal.Revoked, RevocationMarker.classifyUpgrade(403, JSON, ByteArrayInputStream(valid)))
    }

    @Test
    fun `a body of exactly 1024 bytes is read and 1025 bytes is unmarked`() {
        assertEquals(1024, paddedMarker(1024).toByteArray().size)
        assertEquals(RevocationSignal.Revoked, upgrade(JSON, paddedMarker(1024)))
        assertEquals(RevocationSignal.AccessDenied, upgrade(JSON, paddedMarker(1025)))
    }

    @Test
    fun `media types other than application json with at most charset utf-8 are unmarked`() {
        for (type in UNMARKED_TYPES) {
            assertEquals(RevocationSignal.AccessDenied, upgrade(type, MARKED), type.toString())
        }
    }

    @Test
    fun `bodies that are not exactly the marker object are unmarked`() {
        for (body in UNMARKED_BODIES) {
            assertEquals(RevocationSignal.AccessDenied, upgrade(JSON, body), body)
        }
    }

    @Test
    fun `a body that is not valid UTF-8 is unmarked`() {
        val invalid =
            listOf(
                byteArrayOf(0xFF.toByte()) + MARKED.toByteArray(),
                // a lenient decoder turns the raw 0xC0 into U+FFFD, and the object is then a valid marker
                withBytesInValue(0xC0),
                withBytesInValue(0xE2, 0x82),
            )
        for (bytes in invalid) {
            val signal = RevocationMarker.classifyUpgrade(403, JSON, ByteArrayInputStream(bytes))
            assertEquals(RevocationSignal.AccessDenied, signal, bytes.decodeToString())
        }
    }

    @Test
    fun `the marker on another status is not a signal`() {
        for (status in listOf(200, 400, 401, 404, 429, 503)) {
            assertEquals(RevocationSignal.NotRevoked, upgrade(JSON, MARKED, status), "status $status")
        }
    }

    @Test
    fun `close 4403 is revoked and the classifier takes no reason text`() {
        assertEquals(RevocationSignal.Revoked, RevocationMarker.classifyClose(4403))
        val methods = RevocationMarker::class.java.methods
        val classifyClose = methods.single { it.name == "classifyClose" }
        assertEquals(listOf(Int::class.javaPrimitiveType), classifyClose.parameterTypes.toList())
    }

    private companion object {
        const val JSON = "application/json"
        const val MARKED = "{\"error\":\"device_revoked\"}"
        val MARKED_TYPES =
            listOf(
                "application/json",
                "Application/JSON",
                "application/json; charset=utf-8",
                "application/json;charset=UTF-8",
                "APPLICATION/JSON ; Charset=utf-8",
            )
        val UNMARKED_TYPES =
            listOf(
                null,
                "",
                "text/plain",
                "text/html",
                "text/plain; charset=utf-8",
                "application/problem+json",
                "application/json5",
                "application/json; charset=iso-8859-1",
                "application/json; charset=\"utf-8\"",
                "application/json; charset = utf-8",
                "application/json; charset=utf-8; charset=utf-8",
                "application/json; boundary=x",
                "application/json;",
                "application/json, text/plain",
                "application/json; charset=utf8",
                "application/json; charset=UTF_8",
                "application/json; charset=utf-16",
                // characters that String.equalsIgnoreCase folds onto ASCII letters: dotless i, dotted capital I, long s
                "appl\u0131cation/json",
                "appl\u0130cation/json",
                "application/json; char\u017Fet=utf-8",
                "application/json; charset=utf-8\u00A0",
            )
        val UNMARKED_BODIES =
            listOf(
                "",
                "{\"error\":\"unauthorized\"}",
                "{\"error\":\"Device_Revoked\"}",
                "{\"error\":\"DEVICE_REVOKED\"}",
                "{\"error\":\"device_revoked \"}",
                "<html><body>{\"error\":\"device_revoked\"}</body></html>",
                "[{\"error\":\"device_revoked\"}]",
                "{\"error\":\"device_revoked\"} x",
                "{\"error\":\"device_revoked\"}{}",
                "{\"error\":\"device_revoked\",\"error\":\"device_revoked\"}",
                "{\"error\":\"unauthorized\",\"error\":\"device_revoked\"}",
                "{\"error\":[\"device_revoked\"]}",
                "{\"error\":{\"code\":\"device_revoked\"}}",
                "{\"reason\":\"device_revoked\"}",
                "\uFEFF{\"error\":\"device_revoked\"}",
                "\"device_revoked\"",
                "device_revoked",
            )
    }
}
