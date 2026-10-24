// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.core.revoke

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Edge cases for [RevocationSequence] (SAT-PL-72, SAT-PL-74 as the sequence uses the flag): hooks registered from a
 * hook, repeated runs, step 0 failures, every kind of `Throwable`, serialised concurrent runs, and the first-identity
 * rule as the step-0 hook meets it.
 */
class RevocationSequenceOrderingTest {
    private val identity = "a".repeat(64)
    private val other = "b".repeat(64)
    private val order = mutableListOf<String>()
    private val lines = mutableListOf<String>()
    private val storage = FakeStorage()
    private val flag = PersistedRevokedFlag(storage)
    private val sequence = RevocationSequence(flag) { lines += it }

    private fun mark(
        step: RevocationStep,
        name: String = step.name,
    ) = sequence.register(step) { order += name }

    private fun storeFlag() = sequence.register(RevocationStep.PERSIST_FLAG) { flag.set(identity) }

    @Test
    fun `a hook registered during a run joins that run only for a later step and never breaks the run`() {
        storeFlag()
        var registered = false
        sequence.register(RevocationStep.STOP_SENDING) {
            order += "outer"
            if (!registered) {
                registered = true
                mark(RevocationStep.STOP_SENDING, "same-step")
                mark(RevocationStep.STOP_TRAFFIC, "later-step")
                mark(RevocationStep.PERSIST_FLAG, "earlier-step")
            }
        }
        val first = sequence.run()
        assertTrue(first.failures.isEmpty(), first.failures.toString())
        assertEquals(listOf("outer", "later-step"), order)
        order.clear()
        assertTrue(sequence.run().failures.isEmpty())
        assertEquals(listOf("earlier-step", "outer", "same-step", "later-step"), order)
    }

    @Test
    fun `many hooks of one step run in registration order between the hooks of the other steps`() {
        storeFlag()
        for (index in 0 until MANY) {
            mark(RevocationStep.WIPE_CONTENT, "content-%02d".format(index))
            mark(RevocationStep.AUDIT, "audit-%02d".format(index))
            mark(RevocationStep.STOP_SENDING, "sending-%02d".format(index))
        }
        sequence.run()
        val expected =
            listOf("sending", "content", "audit").flatMap { prefix -> (0 until MANY).map { "$prefix-%02d".format(it) } }
        assertEquals(expected, order)
    }

    @Test
    fun `a re-run reports the same failures again and does not accumulate them`() {
        storeFlag()
        sequence.register(RevocationStep.WIPE_SECRETS) { error("keystore busy") }
        val runs = List(THREE) { sequence.run() }
        assertEquals(1, runs[0].failures.size)
        assertEquals(runs[0].failures, runs[1].failures)
        assertEquals(runs[0].failures, runs[2].failures)
        assertEquals(listOf(1, 2, THREE), runs.map { it.run })
        assertEquals(THREE, sequence.runCount)
        assertEquals(identity, storage.value)
        assertTrue(lines.none { identity in it || "keystore busy" in it }, lines.toString())
    }

    @Test
    fun `a step 0 hook that stores the flag and then throws still reports step 0 as failed`() {
        sequence.register(RevocationStep.PERSIST_FLAG) {
            flag.set(identity)
            error("late failure")
        }
        mark(RevocationStep.STOP_SENDING)
        val result = sequence.run()
        assertTrue(result.persistFailed)
        assertEquals(1, result.failures.size)
        assertTrue(flag.isSet())
        assertEquals(listOf("STOP_SENDING"), order)
    }

    @Test
    fun `a step 0 hook that throws before storing is reported next to the missing flag`() {
        sequence.register(RevocationStep.PERSIST_FLAG) { error("cert unreadable") }
        val result = sequence.run()
        assertTrue(result.persistFailed)
        assertEquals(listOf(RevocationStep.PERSIST_FLAG, RevocationStep.PERSIST_FLAG), result.failures.map { it.step })
        assertFalse(result.failures[0].hookType.endsWith("FlagStoredCheck"))
        assertTrue(result.failures[1].hookType.endsWith("FlagStoredCheck"))
        assertFalse(flag.isSet())
    }

    @Test
    fun `an unreadable flag store fails step 0 and steps 1 to 6 still run`() {
        val blind = RevocationSequence(PersistedRevokedFlag(UnreadableStorage())) { lines += it }
        for (step in RevocationStep.entries) blind.register(step) { order += step.name }
        val result = blind.run()
        assertTrue(result.persistFailed)
        assertEquals(RevocationStep.entries.map { it.name }, order)
        assertEquals(listOf("java.lang.IllegalStateException"), result.failures.map { it.errorType })
    }

    @Test
    fun `a hook that throws any kind of Throwable stops no later hook and leaks no message`() {
        val thrown =
            listOf(
                StackOverflowError("SECRET-MSG"),
                OutOfMemoryError("SECRET-MSG"),
                NoClassDefFoundError("SECRET-MSG"),
                ExceptionInInitializerError("SECRET-MSG"),
                AssertionError("SECRET-MSG"),
                InterruptedException("SECRET-MSG"),
                Exception("SECRET-MSG"),
                RuntimeException("SECRET-MSG"),
            )
        storeFlag()
        for (throwable in thrown) sequence.register(RevocationStep.WIPE_SECRETS) { throw throwable }
        mark(RevocationStep.AUDIT)
        val result = sequence.run()
        assertEquals(listOf("AUDIT"), order)
        assertEquals(thrown.map { it.javaClass.name }, result.failures.map { it.errorType })
        assertTrue(lines.none { "SECRET-MSG" in it }, lines.toString())
    }

    @Test
    fun `two concurrent runs never interleave their hooks`() {
        val inside = CountDownLatch(1)
        val release = CountDownLatch(1)
        val trace = Collections.synchronizedList(mutableListOf<String>())
        val finished = Collections.synchronizedList(mutableListOf<Int>())
        storeFlag()
        sequence.register(RevocationStep.STOP_SENDING) {
            trace += Thread.currentThread().name
            if (Thread.currentThread().name == "run-a") {
                inside.countDown()
                release.await(WAIT_SECONDS, TimeUnit.SECONDS)
            }
        }
        sequence.register(RevocationStep.STOP_TRAFFIC) { trace += Thread.currentThread().name }
        try {
            val first = thread(name = "run-a") { finished += sequence.run().run }
            assertTrue(inside.await(WAIT_SECONDS, TimeUnit.SECONDS), "the first run never reached its hook")
            val second = thread(name = "run-b") { finished += sequence.run().run }
            Thread.sleep(SETTLE_MILLIS)
            release.countDown()
            first.join(JOIN_MILLIS)
            second.join(JOIN_MILLIS)
        } finally {
            release.countDown()
        }
        assertEquals(listOf("run-a", "run-a", "run-b", "run-b"), trace)
        assertEquals(listOf(1, 2), finished)
    }

    @Test
    fun `the first readable identity stays when a later run reads another one`() {
        var current = identity
        sequence.register(RevocationStep.PERSIST_FLAG) { flag.set(current) }
        sequence.run()
        current = other
        sequence.run()
        assertEquals(identity, storage.value)
        assertFalse(flag.clearAfterNewEnrolment(identity))
        assertTrue(flag.isSet())
        assertTrue(flag.clearAfterNewEnrolment(other))
        assertFalse(flag.isSet())
    }

    @Test
    fun `a malformed identity from a failed read is replaced by the readable one on the next run`() {
        var current = ""
        sequence.register(RevocationStep.PERSIST_FLAG) { flag.set(current) }
        sequence.run()
        assertTrue(flag.isSet())
        current = identity
        sequence.run()
        assertEquals(identity, storage.value)
        assertFalse(flag.clearAfterNewEnrolment(identity))
        assertTrue(flag.isSet())
    }

    private companion object {
        const val MANY = 50
        const val THREE = 3
        const val WAIT_SECONDS = 10L
        const val SETTLE_MILLIS = 300L
        const val JOIN_MILLIS = 10_000L
    }
}
