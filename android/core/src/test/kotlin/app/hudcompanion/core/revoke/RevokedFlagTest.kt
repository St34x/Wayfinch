// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.core.revoke

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class FakeStorage(
    var value: String? = null,
) : RevokedFlagStorage {
    override fun load(): String? = value

    override fun save(revokedIdentity: String?) {
        value = revokedIdentity
    }
}

class RevokedFlagTest {
    private val revoked = "a".repeat(64)
    private val fresh = "b".repeat(64)
    private val storage = FakeStorage()
    private val flag = PersistedRevokedFlag(storage)

    /** SAT-PL-74: the public surface of the flag port, and of its implementation, has no other way to clear. */
    @Test
    fun `SAT-PL-74 the port offers exactly isSet, set and clearAfterNewEnrolment`() {
        val expected = setOf("isSet", "set", "clearAfterNewEnrolment")
        val port = RevokedFlag::class.java.methods
        assertEquals(expected, port.map { it.name }.toSet())
        assertEquals(3, port.size)
        val own = PersistedRevokedFlag::class.java.methods.filter { it.declaringClass != Any::class.java }
        assertEquals(expected, own.map { it.name }.toSet())
        assertEquals(3, own.size)
        assertTrue(PersistedRevokedFlag::class.java.fields.isEmpty())
    }

    @Test
    fun `SAT-PL-74 the flag stays set when the new identity is the revoked one`() {
        flag.set(revoked)
        assertFalse(flag.clearAfterNewEnrolment(revoked))
        assertTrue(flag.isSet())
        assertEquals(revoked, storage.value)
    }

    @Test
    fun `the revoked identity is compared in lower case`() {
        flag.set(revoked.uppercase())
        assertFalse(flag.clearAfterNewEnrolment(revoked))
        assertTrue(flag.isSet())
    }

    @Test
    fun `a new enrolment with another identity clears the flag`() {
        flag.set(revoked)
        assertTrue(flag.clearAfterNewEnrolment(fresh))
        assertFalse(flag.isSet())
        assertNull(storage.value)
    }

    @Test
    fun `an identity that is not a SPKI SHA-256 in lower-case hex never clears`() {
        flag.set(revoked)
        val bads = listOf("", " ", "b".repeat(63), "b".repeat(65), "B".repeat(64), "g".repeat(64), "$fresh\n")
        for (bad in bads) {
            assertFalse(flag.clearAfterNewEnrolment(bad), "identity '$bad'")
        }
        assertTrue(flag.isSet())
    }

    @Test
    fun `SAT-PL-74 a later set keeps the first revoked identity`() {
        flag.set(revoked)
        flag.set(fresh)
        assertEquals(revoked, storage.value)
        assertFalse(flag.clearAfterNewEnrolment(revoked))
        assertTrue(flag.isSet())
    }

    /** A malformed first record (a transient identity-read failure) must not lock out the real identity. */
    @Test
    fun `SAT-PL-74 a well-formed identity replaces a malformed first one and cannot clear`() {
        flag.set("")
        flag.set(revoked)
        assertEquals(revoked, storage.value)
        assertFalse(flag.clearAfterNewEnrolment(revoked))
        assertTrue(flag.isSet())
        assertEquals(revoked, storage.value)
    }

    @Test
    fun `a malformed later set keeps the stored well-formed identity`() {
        flag.set(revoked)
        flag.set("")
        flag.set("junk")
        assertEquals(revoked, storage.value)
    }

    @Test
    fun `an upper-case first identity counts as well-formed and stays`() {
        flag.set(revoked.uppercase())
        flag.set(fresh)
        assertEquals(revoked.uppercase(), storage.value)
        assertFalse(flag.clearAfterNewEnrolment(revoked))
    }

    @Test
    fun `a malformed set after a malformed one keeps the flag set`() {
        flag.set("junk")
        flag.set("other junk")
        assertTrue(flag.isSet())
        assertTrue(PersistedRevokedFlag(storage).isSet())
    }

    @Test
    fun `the flag survives a new instance over the same storage and clearing an unset flag is a no-op`() {
        assertFalse(flag.isSet())
        assertTrue(flag.clearAfterNewEnrolment(fresh))
        flag.set(revoked)
        assertTrue(PersistedRevokedFlag(storage).isSet())
    }
}
