// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.core.revoke

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** An in-process `Error` that is not an `Exception` (a broken static init, a missing class, an assertion). */
class PlantedError : Error()

class UnreadableStorage : RevokedFlagStorage {
    override fun load(): String? = error("preference store unreadable")

    override fun save(revokedIdentity: String?): Unit = error("preference store unreadable")
}

class RevocationSequenceTest {
    private val identity = "a".repeat(64)
    private val order = mutableListOf<String>()
    private val lines = mutableListOf<String>()
    private val storage = FakeStorage()
    private val flag = PersistedRevokedFlag(storage)
    private val sequence = RevocationSequence(flag) { lines += it }

    private fun record(
        step: RevocationStep,
        name: String = step.name,
        sequence: RevocationSequence = this.sequence,
    ) = sequence.register(step) {
        order += name
        if (step == RevocationStep.PERSIST_FLAG) flag.set(identity)
    }

    private fun registerAll(sequence: RevocationSequence = this.sequence) {
        // registered out of order on purpose; two hooks on step 3 keep their registration order
        for (step in RevocationStep.entries.reversed()) record(step, sequence = sequence)
        record(RevocationStep.WIPE_SECRETS, "WIPE_SECRETS-second", sequence)
    }

    private val inOrder =
        listOf(
            "PERSIST_FLAG",
            "STOP_SENDING",
            "STOP_TRAFFIC",
            "WIPE_SECRETS",
            "WIPE_SECRETS-second",
            "CLEAR_SHOWN",
            "WIPE_CONTENT",
            "AUDIT",
        )

    /** SAT-PL-72: hooks run 0->6 once, in order. */
    @Test
    fun `SAT-PL-72 run executes steps 0 to 6 once in order`() {
        registerAll()
        val result = sequence.run()
        assertEquals(inOrder, order)
        assertEquals(7, RevocationStep.entries.size)
        assertEquals(listOf(0, 1, 2, 3, 4, 5, 6), RevocationStep.entries.map { it.ordinal })
        assertEquals(1, result.run)
        assertTrue(result.failures.isEmpty())
        assertFalse(result.persistFailed)
    }

    /** SAT-PL-72: a throwing hook is logged by type only and the next one runs. */
    @Test
    fun `SAT-PL-72 a throwing hook is logged by type only and the next hook runs`() {
        sequence.register(RevocationStep.STOP_SENDING) { error("PLANTED-MESSAGE-TEXT") }
        registerAll()
        sequence.register(RevocationStep.WIPE_SECRETS) { throw IllegalStateException("PLANTED-TOKEN") }
        val result = sequence.run()
        assertEquals(inOrder, order)
        assertEquals(listOf(RevocationStep.STOP_SENDING, RevocationStep.WIPE_SECRETS), result.failures.map { it.step })
        assertEquals("java.lang.IllegalStateException", result.failures[0].errorType)
        val log = lines.joinToString("\n")
        assertTrue("step=1" in log && "step=3" in log && "java.lang.IllegalStateException" in log)
        assertFalse("PLANTED" in log)
    }

    /** SAT-PL-72: an unfinished hook (what the stdlib stub call throws) or any other `Error` stops no later hook. */
    @Test
    fun `SAT-PL-72 a hook throwing NotImplementedError or another Error is logged and the next hook runs`() {
        sequence.register(RevocationStep.STOP_TRAFFIC) { throw NotImplementedError("PLANTED-STUB") }
        registerAll()
        sequence.register(RevocationStep.WIPE_SECRETS) { throw PlantedError() }
        val result = sequence.run()
        assertEquals(inOrder, order)
        val types = result.failures.map { it.errorType }
        assertEquals(listOf("kotlin.NotImplementedError", PlantedError::class.java.name), types)
        assertEquals(listOf(RevocationStep.STOP_TRAFFIC, RevocationStep.WIPE_SECRETS), result.failures.map { it.step })
        assertFalse("PLANTED" in lines.joinToString("\n"))
    }

    /** SAT-PL-72: a failing step 0 still runs 1-6 and the result reports it. */
    @Test
    fun `SAT-PL-72 a failing step 0 still runs steps 1 to 6 and reports it`() {
        sequence.register(RevocationStep.PERSIST_FLAG) { error("disk full") }
        registerAll()
        val result = sequence.run()
        assertEquals(inOrder, order)
        assertTrue(result.persistFailed)
        assertEquals(listOf(RevocationStep.PERSIST_FLAG), result.failures.map { it.step })
    }

    /** SAT-PL-72: step 0 is enforced by the sequence: no hook stored the flag -> step 0 failed, 1-6 still ran. */
    @Test
    fun `SAT-PL-72 a run that leaves the flag unset reports a failed step 0`() {
        for (step in RevocationStep.entries.drop(1)) record(step)
        val result = sequence.run()
        assertEquals(inOrder.filter { it != "PERSIST_FLAG" && it != "WIPE_SECRETS-second" }, order)
        assertTrue(result.persistFailed)
        assertEquals(1, result.failures.size)
        assertTrue(result.failures[0].hookType.endsWith("FlagStoredCheck"))
        assertFalse(flag.isSet())
    }

    /** SAT-PL-72: a crash after step 1 leaves the flag set; the next start re-runs 0-6 before anything connects. */
    @Test
    fun `SAT-PL-72 a crash after step 1 re-runs steps 0 to 6 at the next start`() {
        // the killed first process got through its step-0 and step-1 hooks, then died without throwing anything
        val killed = RevocationSequence(PersistedRevokedFlag(storage)) { lines += it }
        record(RevocationStep.PERSIST_FLAG, "first-process:PERSIST_FLAG", killed)
        record(RevocationStep.STOP_SENDING, "first-process:STOP_SENDING", killed)
        assertFalse(killed.run().persistFailed)
        assertEquals(listOf("first-process:PERSIST_FLAG", "first-process:STOP_SENDING"), order)
        order.clear()

        val restarted = RevocationSequence(PersistedRevokedFlag(storage)) { lines += it }
        for (step in RevocationStep.entries) record(step, sequence = restarted)
        val result = restarted.resumeAtStart()
        assertNotNull(result)
        assertEquals(RevocationStep.entries.map { it.name }, order)
        assertEquals(1, restarted.runCount)
    }

    @Test
    fun `resumeAtStart runs no hook while the flag is clear`() {
        registerAll()
        assertNull(sequence.resumeAtStart())
        assertTrue(order.isEmpty())
        assertEquals(0, sequence.runCount)
    }

    /** SAT-PL-64 input for T1.24b: an unreadable flag store throws out of resumeAtStart and runs nothing. */
    @Test
    fun `resumeAtStart throws and runs no hook when the flag cannot be read`() {
        val blind = RevocationSequence(PersistedRevokedFlag(UnreadableStorage())) { lines += it }
        registerAll(blind)
        assertThrows(IllegalStateException::class.java) { blind.resumeAtStart() }
        assertTrue(order.isEmpty())
        assertEquals(0, blind.runCount)
    }

    /** SAT-PL-72: a re-run repeats every hook (counted), and the end state equals the one after one run. */
    @Test
    fun `SAT-PL-72 a re-run repeats the hooks and counts the run`() {
        var wipes = 0
        val history = mutableListOf("row")
        sequence.register(RevocationStep.PERSIST_FLAG) { flag.set(identity) }
        sequence.register(RevocationStep.WIPE_CONTENT) {
            wipes++
            history.clear()
        }
        val first = sequence.run()
        assertEquals(1, wipes)
        assertTrue(history.isEmpty())
        val second = sequence.run()
        assertEquals(listOf(1, 2), listOf(first.run, second.run))
        assertEquals(2, sequence.runCount)
        assertEquals(2, wipes)
        assertTrue(history.isEmpty())
        assertEquals(identity, storage.value)
    }

    @Test
    fun `a throwing logger does not stop the sequence`() {
        val loud = RevocationSequence(flag) { error("log sink closed") }
        loud.register(RevocationStep.STOP_SENDING) { error("boom") }
        loud.register(RevocationStep.AUDIT) { order += "AUDIT" }
        loud.run()
        assertEquals(listOf("AUDIT"), order)
    }

    @Test
    fun `a logger throwing an Error does not stop the sequence`() {
        val broken = RevocationSequence(flag) { throw PlantedError() }
        broken.register(RevocationStep.STOP_SENDING) { error("boom") }
        broken.register(RevocationStep.AUDIT) { order += "AUDIT" }
        assertEquals(2, broken.run().failures.size)
        assertEquals(listOf("AUDIT"), order)
    }

    /**
     * SAT-PL-73 (hook half, as far as `:core` can pin it): `false` runs nothing, `true` runs the sequence once. The
     * real classifier wiring ("AccessDenied runs the sequence" as planted negative) is T1.24a's acceptance.
     */
    @Test
    fun `SAT-PL-73 only a Revoked classifier result runs the sequence`() {
        registerAll()
        for (signal in listOf(Signal.NotRevoked, Signal.AccessDenied, Signal.NotRevoked, Signal.AccessDenied)) {
            assertNull(feed(signal), "$signal")
        }
        assertTrue(order.isEmpty())
        assertEquals(0, sequence.runCount)
        assertNotNull(feed(Signal.Revoked))
        assertEquals(inOrder, order)
        assertEquals(1, sequence.runCount)
    }

    /** The wiring of T1.24a (7c): the classifier's answer is reduced to "is it a revocation". */
    private fun feed(signal: Signal): RevocationResult? = sequence.onSignal(signal == Signal.Revoked)

    /** The shape of T1.24d's answers; `:core` has no dependency on `:protocol`, so the test owns this fake. */
    private enum class Signal { NotRevoked, AccessDenied, Revoked }
}
