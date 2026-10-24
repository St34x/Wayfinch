// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.revoke

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.io.IOException
import java.io.InputStream

/** SAT-PL-71 (T1.24d): the classifier reads at most 1025 bytes from a 10 MiB body. */
class RevocationMarkerBodyCapTest {
    /** A body of [size] bytes that starts with the marker and counts every byte handed out. */
    private class CountingBody(
        private val size: Int,
    ) : InputStream() {
        private val head = "{\"error\":\"device_revoked\"}".toByteArray()
        var served = 0L
            private set

        override fun read(): Int {
            if (served >= size) return -1
            val byte = if (served < head.size) head[served.toInt()].toInt() else ' '.code
            served += 1
            return byte
        }
    }

    /** A body that fails after its first bytes. */
    private class FailingBody : InputStream() {
        private var served = 0

        override fun read(): Int {
            served += 1
            if (served > 4) throw IOException("connection reset")
            return '{'.code
        }
    }

    /** A source that was closed under the reader, as OkHttp's is: reading throws an [IllegalStateException]. */
    private class ClosedSource : InputStream() {
        override fun read(): Int = error("closed")
    }

    /**
     * A source that serves the complete marker and then breaks the contract by returning 0 for a non-empty request;
     * it throws after 50 reads, so a reader that keeps asking ends in an [IllegalStateException].
     */
    private class ZeroSource : InputStream() {
        private val head = "{\"error\":\"device_revoked\"}".toByteArray()
        var reads = 0
            private set

        override fun read(): Int = 0

        override fun read(
            buffer: ByteArray,
            offset: Int,
            length: Int,
        ): Int {
            reads += 1
            check(reads <= MAX_ZERO_READS) { "the reader keeps asking a source that returns 0" }
            if (reads > 1) return 0
            head.copyInto(buffer, offset)
            return head.size
        }
    }

    /** A marked body that records whether the classifier closed it. */
    private class WatchedBody : InputStream() {
        private val source = "{\"error\":\"device_revoked\"}".byteInputStream()
        var closed = false
            private set

        override fun read(): Int = source.read()

        override fun close() {
            closed = true
        }
    }

    @Test
    fun `at most 1025 bytes are read from a 10 MiB marked body and it is unmarked`() {
        val body = CountingBody(10 * 1024 * 1024)
        val signal = RevocationMarker.classifyUpgrade(403, "application/json", body)
        assertEquals(RevocationSignal.AccessDenied, signal)
        assertTrue(body.served <= 1025, "read ${body.served} bytes")
    }

    @Test
    fun `a short marked counting body is read to its end and is revoked`() {
        val body = CountingBody(100)
        assertEquals(RevocationSignal.Revoked, RevocationMarker.classifyUpgrade(403, "application/json", body))
        assertEquals(100L, body.served)
    }

    @Test
    fun `the body of a non-JSON 403 or of another status is not read`() {
        val plain = CountingBody(100)
        assertEquals(RevocationSignal.AccessDenied, RevocationMarker.classifyUpgrade(403, "text/plain", plain))
        val other = CountingBody(100)
        assertEquals(RevocationSignal.NotRevoked, RevocationMarker.classifyUpgrade(401, "application/json", other))
        assertEquals(0L, plain.served + other.served)
    }

    @Test
    fun `a read error is an unmarked 403 and does not escape`() {
        val signal = RevocationMarker.classifyUpgrade(403, "application/json", FailingBody())
        assertEquals(RevocationSignal.AccessDenied, signal)
    }

    @Test
    fun `a source closed under the reader is an unmarked 403 and does not escape`() {
        val signal = RevocationMarker.classifyUpgrade(403, "application/json", ClosedSource())
        assertEquals(RevocationSignal.AccessDenied, signal)
    }

    @Test
    fun `a source that returns 0 after a complete marker is an unmarked 403 and is not asked again`() {
        val body = ZeroSource()
        assertEquals(RevocationSignal.AccessDenied, RevocationMarker.classifyUpgrade(403, "application/json", body))
        assertEquals(2, body.reads)
    }

    @Test
    fun `the classifier never closes the body it was given`() {
        for (status in listOf(403, 401)) {
            val body = WatchedBody()
            RevocationMarker.classifyUpgrade(status, "application/json", body)
            assertFalse(body.closed, "status $status")
        }
    }

    private companion object {
        const val MAX_ZERO_READS = 50
    }
}
