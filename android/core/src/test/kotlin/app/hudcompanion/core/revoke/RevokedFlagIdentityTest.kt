// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.core.revoke

import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Edge cases for [PersistedRevokedFlag] identities (SAT-PL-74): which stored text counts as a well-formed SPKI
 * SHA-256, the letter case of stored and new identities, look-alike digits, storage that fails to write, and two
 * threads setting the first identity at once.
 */
class RevokedFlagIdentityTest {
    private val revoked = "0123456789abcdef".repeat(4)
    private val fresh = "fedcba9876543210".repeat(4)

    private fun flagOver(stored: String?): Pair<FakeStorage, PersistedRevokedFlag> {
        val storage = FakeStorage(stored)
        return storage to PersistedRevokedFlag(storage)
    }

    @Test
    fun `a stored text that is not exactly 64 hex digits is replaced by the next set`() {
        val malformed =
            listOf(
                "",
                " ",
                "\n",
                "null",
                "a".repeat(63),
                "a".repeat(65),
                revoked + "\n",
                "\n" + revoked,
                " $revoked",
                revoked + "\u0000",
                "g".repeat(64),
                "a".repeat(63) + "g",
                "0x" + "a".repeat(62),
                "sha256:" + "a".repeat(57),
                "\uFF11".repeat(64), // fullwidth digit one
                "\u0663".repeat(64), // Arabic-Indic digit three
                "\u0430".repeat(64), // Cyrillic a
            )
        assertAll(
            malformed.map { text ->
                Executable {
                    val (storage, flag) = flagOver(text)
                    flag.set(revoked)
                    assertEquals(revoked, storage.value, "stored '$text'")
                    assertFalse(flag.clearAfterNewEnrolment(revoked), "stored '$text'")
                }
            },
        )
    }

    @Test
    fun `a stored SPKI hash in any letter case is kept against a later set`() {
        val stored = listOf(revoked, revoked.uppercase(), "AbCdEf0123456789".repeat(4))
        for (text in stored) {
            val (storage, flag) = flagOver(text)
            flag.set(fresh)
            flag.set("")
            assertEquals(text, storage.value)
            assertFalse(flag.clearAfterNewEnrolment(text.lowercase()), "stored $text")
        }
    }

    @Test
    fun `a new identity must be lower-case hex, so the upper-case revoked hash cannot be cleared by itself`() {
        val (storage, flag) = flagOver(revoked.uppercase())
        assertFalse(flag.clearAfterNewEnrolment(revoked))
        assertFalse(flag.clearAfterNewEnrolment(revoked.uppercase()))
        assertFalse(flag.clearAfterNewEnrolment(fresh.uppercase()))
        assertEquals(revoked.uppercase(), storage.value)
        assertTrue(flag.clearAfterNewEnrolment(fresh))
        assertNull(storage.value)
    }

    @Test
    fun `a new identity that only looks like hex never clears`() {
        val (storage, flag) = flagOver(revoked)
        val lookalikes =
            listOf(
                "\uFF11".repeat(64),
                "\u0663".repeat(64),
                "\u0430".repeat(64),
                "$fresh ",
                " $fresh",
                "$fresh\u0000",
                "0x" + fresh.take(62),
                fresh.take(63),
                fresh + "0",
                "",
            )
        assertAll(
            lookalikes.map { text ->
                Executable { assertFalse(flag.clearAfterNewEnrolment(text), "new identity '$text'") }
            },
        )
        assertEquals(revoked, storage.value)
    }

    /** Known limit, pinned so a change is seen: with no well-formed identity stored there is nothing to compare. */
    @Test
    fun `a flag that holds only a malformed identity is cleared by any well-formed new identity`() {
        val (storage, flag) = flagOver("")
        assertTrue(flag.clearAfterNewEnrolment(revoked))
        assertNull(storage.value)
    }

    private class FailingSave(
        var value: String?,
    ) : RevokedFlagStorage {
        override fun load(): String? = value

        override fun save(revokedIdentity: String?): Unit = error("disk full")
    }

    @Test
    fun `a storage that cannot write makes set and clear throw and leaves the flag as it was`() {
        val empty = FailingSave(null)
        assertThrows(IllegalStateException::class.java) { PersistedRevokedFlag(empty).set(revoked) }
        assertFalse(PersistedRevokedFlag(empty).isSet())

        val held = FailingSave(revoked)
        assertThrows(IllegalStateException::class.java) { PersistedRevokedFlag(held).clearAfterNewEnrolment(fresh) }
        assertTrue(PersistedRevokedFlag(held).isSet())
        assertEquals(revoked, held.value)
    }

    @Test
    fun `a clear refused for the revoked identity never touches the storage`() {
        val held = FailingSave(revoked)
        val flag = PersistedRevokedFlag(held)
        assertFalse(flag.clearAfterNewEnrolment(revoked))
        assertFalse(flag.clearAfterNewEnrolment(""))
        flag.set(fresh)
        assertEquals(revoked, held.value)
    }

    /** A storage whose load of an empty flag waits for a second reader, to open the check-then-write window. */
    private class RacingStorage : RevokedFlagStorage {
        private val arrived = CountDownLatch(2)
        val saves: MutableList<String?> = Collections.synchronizedList(mutableListOf())

        @Volatile
        var value: String? = null

        override fun load(): String? {
            val seen = value
            if (seen == null) {
                // a second reader arriving here releases both at once; a lone reader waits out the timeout
                arrived.countDown()
                arrived.await(PEER_MILLIS, TimeUnit.MILLISECONDS)
            }
            return seen
        }

        override fun save(revokedIdentity: String?) {
            saves += revokedIdentity
            value = revokedIdentity
        }

        private companion object {
            const val PEER_MILLIS = 400L
        }
    }

    @Test
    fun `two threads setting the first identity at once store exactly one of them`() {
        val racing = RacingStorage()
        val flag = PersistedRevokedFlag(racing)
        val first = thread { flag.set(revoked) }
        val second = thread { flag.set(fresh) }
        first.join(JOIN_MILLIS)
        second.join(JOIN_MILLIS)
        assertEquals(1, racing.saves.size, racing.saves.toString())
        assertTrue(racing.value == revoked || racing.value == fresh)
    }

    private companion object {
        const val JOIN_MILLIS = 10_000L
    }
}
