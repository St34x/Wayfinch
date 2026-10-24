// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.Base64
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * SAT-PL-33 / SAT-PL-34: the `spki` in the body and the fingerprint shown beside it come from one private copy of the
 * key bytes. The copy only matters while another thread changes the caller's array during [EnrolRequest.of], so the
 * test changes the array from a second thread for a bounded time and compares the two outputs of every call.
 */
class EnrolRequestConcurrentMutationTest {
    private val signature = ByteArray(SIGNATURE_BYTES) { (it * 7 + 1).toByte() }
    private val spkiField = Regex("\"spki\":\"([A-Za-z0-9+/=]+)\"")

    @Test
    fun `a key changed by another thread during the call cannot split the sent spki from the shown fingerprint`() {
        val first = EnrolVectors.spki()
        val second = ByteArray(first.size) { (it * 31 + 7).toByte() }
        val shared = first.copyOf()
        val stop = AtomicBoolean(false)
        val flipper =
            thread(isDaemon = true) {
                while (!stop.get()) {
                    System.arraycopy(second, 0, shared, 0, shared.size)
                    System.arraycopy(first, 0, shared, 0, shared.size)
                }
            }
        var calls = 0
        var diverged = 0
        try {
            val deadline = System.nanoTime() + DURATION_NANOS
            while (System.nanoTime() < deadline) {
                val ready = EnrolRequest.of(shared, Payloads.CODE, signature) as EnrolRequest.Ready
                val sent =
                    Base64.getDecoder().decode(
                        spkiField
                            .find(ready.body)
                            ?.groupValues
                            ?.get(1)
                            .orEmpty(),
                    )
                if (keyFingerprint(sent) != ready.fingerprint) diverged += 1
                calls += 1
            }
        } finally {
            stop.set(true)
            flipper.join(JOIN_MILLIS)
        }
        assertTrue(calls > 0)
        assertEquals(0, diverged, "$diverged of $calls bodies carry another key than the fingerprint shown")
    }

    private companion object {
        const val SIGNATURE_BYTES = 71
        const val DURATION_NANOS = 400_000_000L
        const val JOIN_MILLIS = 2_000L
    }
}
