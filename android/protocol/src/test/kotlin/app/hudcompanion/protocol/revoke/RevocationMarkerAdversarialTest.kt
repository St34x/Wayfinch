// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.revoke

import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable
import java.io.ByteArrayInputStream
import java.io.IOException
import java.io.InputStream
import java.io.UncheckedIOException

/**
 * Edge cases for the revocation marker (SAT-PL-70, SAT-PL-71, SAT-PL-73): ASCII case and non-ASCII look-alikes in the
 * media type and in the body, charset spellings, a lenient UTF-8 decoder, JSON-equivalent spellings, nesting, chunked
 * reads, and statuses and close codes outside the table.
 */
class RevocationMarkerAdversarialTest {
    private fun upgradeBytes(
        contentType: String?,
        body: ByteArray,
        status: Int = 403,
    ): RevocationSignal = RevocationMarker.classifyUpgrade(status, contentType, ByteArrayInputStream(body))

    private fun upgrade(
        contentType: String?,
        body: String,
    ): RevocationSignal = upgradeBytes(contentType, body.toByteArray(Charsets.UTF_8))

    private fun assertTypes(
        expected: RevocationSignal,
        types: List<String>,
    ) = assertAll(types.map { type -> Executable { assertEquals(expected, upgrade(type, MARKED), type) } })

    private fun assertBodies(
        expected: RevocationSignal,
        bodies: List<String>,
    ) = assertAll(bodies.map { body -> Executable { assertEquals(expected, upgrade(JSON, body), body) } })

    /** The marker object with a second member whose string value holds the raw [bytes], or one raw key. */
    private fun withRawValue(vararg bytes: Int): ByteArray =
        "{\"error\":\"device_revoked\",\"x\":\"".toByteArray() + raw(bytes) + "\"}".toByteArray()

    private fun withRawKey(vararg bytes: Int): ByteArray =
        "{\"error\":\"device_revoked\",\"".toByteArray() + raw(bytes) + "\":1}".toByteArray()

    private fun raw(bytes: IntArray): ByteArray = ByteArray(bytes.size) { bytes[it].toByte() }

    @Test
    fun `a media type in mixed ASCII case with tabs around the parameter is revoked`() {
        val types =
            listOf(
                "aPpLiCaTiOn/JsOn",
                "APPLICATION/JSON; CHARSET=UTF-8",
                "application/json; charset=Utf-8",
                "application/json;\tcharset=utf-8",
                "application/json\t;\tcharset=utf-8",
                "application/json; charset=utf-8",
            )
        assertTypes(RevocationSignal.Revoked, types)
    }

    @Test
    fun `a media type with a non-ASCII look-alike or a control character is unmarked`() {
        val types =
            listOf(
                "\u0430pplication/json", // Cyrillic a
                "applicati\u043En/json", // Cyrillic o
                "applicati\u03BFn/json", // Greek omicron
                "\uFF41pplication/json", // fullwidth a
                "application\uFF0Fjson", // fullwidth solidus
                "application/j\u017Fon", // long s folds to s under equalsIgnoreCase
                "application/json\u200B",
                "application/\u200Bjson",
                "application/json;\u200Bcharset=utf-8",
                "application/json; charset=utf-8\u0000",
                "application/json\u0000",
                "application/json\r\n",
                "application/json\nX-Injected: 1",
                "application/json; charset=utf-\u0038\u200B",
                "application/json; charset=\u0075tf-8\uFEFF",
            )
        assertTypes(RevocationSignal.AccessDenied, types)
    }

    @Test
    fun `a charset other than utf-8 spelled in any way, or more than one parameter, is unmarked`() {
        val types =
            listOf(
                "application/json; charset=utf8",
                "application/json; charset=UTF8",
                "application/json; charset=us-ascii",
                "application/json; charset=latin1",
                "application/json; charset=utf-32",
                "application/json; charset=",
                "application/json; charset",
                "application/json; charset=utf-8;",
                "application/json; foo=bar; charset=utf-8",
                "application/json; charset=utf-8; foo=bar",
                "application/json; charset=utf-8, text/html",
                "application/json charset=utf-8",
                "application/json/charset=utf-8",
            )
        assertTypes(RevocationSignal.AccessDenied, types)
    }

    @Test
    fun `a body is unmarked when its bytes are not strict UTF-8, wherever the bad bytes sit`() {
        val bodies =
            listOf(
                withRawValue(0xC0, 0x80), // overlong NUL
                withRawValue(0xE0, 0x80, 0xAF), // overlong solidus
                withRawValue(0xED, 0xA0, 0x80), // an encoded high surrogate
                withRawValue(0xED, 0xA0, 0xBD, 0xED, 0xB8, 0x80), // a CESU-8 surrogate pair
                withRawValue(0xF4, 0x90, 0x80, 0x80), // above U+10FFFF
                withRawValue(0xF8, 0x88, 0x80, 0x80, 0x80), // five-byte form
                withRawValue(0x80), // a lone continuation byte
                withRawValue(0xF0, 0x9F, 0x98), // a truncated four-byte sequence
                withRawValue(0xFF, 0xFE),
                withRawKey(0xC0, 0x80),
                withRawKey(0xFF),
                MARKED.toByteArray() + raw(intArrayOf(0xC3)), // a truncated sequence after the object
                MARKED.toByteArray() + raw(intArrayOf(0x80)),
            )
        val denied = RevocationSignal.AccessDenied
        assertAll(
            bodies.map { body ->
                Executable { assertEquals(denied, upgradeBytes(JSON, body), body.decodeToString()) }
            },
        )
    }

    @Test
    fun `a UTF-16 body is unmarked whatever the charset header says`() {
        val text = "{\"error\":\"device_revoked\"}"
        val bom = byteArrayOf(0xFF.toByte(), 0xFE.toByte())
        for (type in listOf(JSON, "application/json; charset=utf-8")) {
            assertEquals(RevocationSignal.AccessDenied, upgradeBytes(type, bom + text.toByteArray(Charsets.UTF_16LE)))
            assertEquals(RevocationSignal.AccessDenied, upgradeBytes(type, text.toByteArray(Charsets.UTF_16LE)))
            assertEquals(RevocationSignal.AccessDenied, upgradeBytes(type, text.toByteArray(Charsets.UTF_16BE)))
        }
    }

    @Test
    fun `valid two three and four byte characters in another member keep the marker revoked`() {
        val values = listOf(intArrayOf(0xC3, 0xA9), intArrayOf(0xE2, 0x82, 0xAC), intArrayOf(0xF0, 0x9F, 0x98, 0x80))
        for (value in values) {
            assertEquals(RevocationSignal.Revoked, upgradeBytes(JSON, withRawValue(*value)), value.joinToString())
        }
    }

    @Test
    fun `a look-alike of the key or of the value is unmarked`() {
        val bodies =
            listOf(
                "{\"error\":\"d\u0435vice_revoked\"}", // Cyrillic e
                "{\"error\":\"d\u00E9vice_revoked\"}",
                "{\"error\":\"device\uFF3Frevoked\"}", // fullwidth low line
                "{\"error\":\"device_revoked\u200B\"}",
                "{\"error\":\"device_revoked\u00A0\"}",
                "{\"error\":\" device_revoked\"}",
                "{\"error\":\"device_revoked\\u0000\"}",
                "{\"error\":\"device_revoked\\uD800\"}",
                "{\"\u0435rror\":\"device_revoked\"}",
                "{\"error\u200B\":\"device_revoked\"}",
                "{\" error\":\"device_revoked\"}",
                "{\"Error\":\"device_revoked\"}",
                "{\"ERROR\":\"device_revoked\"}",
                "{\"error\":\"DEVICE_revoked\"}",
                "{\"error\":\"device_REVOKED\"}",
                "{\"error\":\"Device_Revoked\"}",
            )
        assertBodies(RevocationSignal.AccessDenied, bodies)
    }

    @Test
    fun `a duplicate error key is unmarked in either order, also when one spelling is a unicode escape`() {
        val bodies =
            listOf(
                "{\"error\":\"device_revoked\",\"\\u0065rror\":\"x\"}",
                "{\"error\":\"x\",\"\\u0065rror\":\"device_revoked\"}",
                "{\"\\u0065rror\":\"device_revoked\",\"error\":\"device_revoked\"}",
                "{\"error\":\"device_revoked\",\"error\":\"device_revoked\"}",
            )
        assertBodies(RevocationSignal.AccessDenied, bodies)
    }

    @Test
    fun `the marker is the JSON string device_revoked, however JSON spells it`() {
        val bodies =
            listOf(
                "{\"error\":\"device\\u005frevoked\"}",
                "{\"\\u0065rror\":\"device_revoked\"}",
                "{\"error\":\"device_revoked\",\"x\":{\"y\":1}}",
                "{\"error\":\"device_revoked\",\"x\":[1,2]}",
                "{\"error\":\"device_revoked\",\"x\":null,\"y\":true,\"z\":1.5e3}",
                "\r\n\t {\"error\":\"device_revoked\"} \r\n",
            )
        assertBodies(RevocationSignal.Revoked, bodies)
    }

    @Test
    fun `a body that is not the plain JSON grammar is unmarked`() {
        val bodies =
            listOf(
                "{\"error\":null}",
                "{\"error\":true}",
                "{\"error\":0}",
                "{\"error\":device_revoked}",
                "{'error':'device_revoked'}",
                "{\"error\":\"device_revoked\",}",
                "{\"error\":\"device_revoked\"",
                "{\"error\":\"device_revoked\" \"x\":1}",
                "{\"error\":\"device_revoked\"} // x",
                "/* x */{\"error\":\"device_revoked\"}",
                "{\"error\":\"device_revoked\",\"x\":NaN}",
                "{\"error\":\"device_revoked\",\"x\":\"\u0001\"}",
                "{\"error\":\"device_revoked\",\"x\":{\"y\":{\"z\":1}}}",
                "{\"error\":{\"error\":\"device_revoked\"}}",
                "{}",
                "null",
                "0",
            )
        assertBodies(RevocationSignal.AccessDenied, bodies)
    }

    /** Reads at most [chunk] bytes per call and counts them. */
    private class Chunked(
        private val data: ByteArray,
        private val chunk: Int,
    ) : InputStream() {
        private var pos = 0
        var served = 0
            private set

        override fun read(): Int = if (pos < data.size) data[pos++].toInt() and BYTE_MASK else -1

        override fun read(
            buffer: ByteArray,
            offset: Int,
            length: Int,
        ): Int {
            val count = minOf(chunk, length, data.size - pos)
            if (count <= 0) return -1
            data.copyInto(buffer, offset, pos, pos + count)
            pos += count
            served += count
            return count
        }

        private companion object {
            const val BYTE_MASK = 0xFF
        }
    }

    /** The marker with spaces after its first brace, [size] bytes in all, split into reads of [chunk] bytes. */
    private fun chunkedMarker(
        size: Int,
        chunk: Int,
    ): RevocationSignal {
        val text = "{" + " ".repeat(size - MARKED.length) + MARKED.substring(1)
        return RevocationMarker.classifyUpgrade(403, JSON, Chunked(text.toByteArray(), chunk))
    }

    @Test
    fun `the body is read to the same verdict however the stream splits it`() {
        for (chunk in listOf(1, 2, 7, 64, MAX_BODY, 4096)) {
            assertEquals(RevocationSignal.Revoked, chunkedMarker(MAX_BODY, chunk), "1024 bytes, chunk $chunk")
            assertEquals(RevocationSignal.AccessDenied, chunkedMarker(MAX_BODY + 1, chunk), "1025 bytes, chunk $chunk")
        }
    }

    @Test
    fun `an empty stream and a stream with only whitespace are unmarked`() {
        assertEquals(RevocationSignal.AccessDenied, upgradeBytes(JSON, ByteArray(0)))
        assertEquals(RevocationSignal.AccessDenied, upgradeBytes(JSON, " \r\n\t".toByteArray()))
    }

    /** A stream error that is neither an IOException nor an IllegalStateException is not swallowed (documented). */
    @Test
    fun `an unexpected stream exception escapes the classifier, it is never turned into a signal`() {
        val failing =
            object : InputStream() {
                override fun read(): Int = throw UncheckedIOException(IOException("stream"))
            }
        assertThrows(UncheckedIOException::class.java) { RevocationMarker.classifyUpgrade(403, JSON, failing) }
    }

    @Test
    fun `only status 403 reads the body, every other status is not revoked and leaves it unread`() {
        val statuses = listOf(Int.MIN_VALUE, -1, 0, 200, 302, 402, 404, 407, 451, 499, 500, 4030, 40300, Int.MAX_VALUE)
        for (status in statuses) {
            val body = Chunked(MARKED.toByteArray(), MAX_BODY)
            val signal = RevocationMarker.classifyUpgrade(status, JSON, body)
            assertEquals(RevocationSignal.NotRevoked, signal, "status $status")
            assertEquals(0, body.served, "status $status read the body")
        }
    }

    @Test
    fun `a sweep of every 16-bit close code and of out-of-range values finds only 4403 revoked`() {
        val signals = (0..MAX_CLOSE).filter { RevocationMarker.classifyClose(it) == RevocationSignal.Revoked }
        assertEquals(listOf(CLOSE_REVOKED), signals)
        for (code in listOf(Int.MIN_VALUE, -CLOSE_REVOKED, 44030, CLOSE_REVOKED + MAX_CLOSE + 1, Int.MAX_VALUE)) {
            assertEquals(RevocationSignal.NotRevoked, RevocationMarker.classifyClose(code), "close $code")
        }
    }

    private companion object {
        const val JSON = "application/json"
        const val MARKED = "{\"error\":\"device_revoked\"}"
        const val MAX_BODY = 1024
        const val CLOSE_REVOKED = 4403
        const val MAX_CLOSE = 65535
    }
}
