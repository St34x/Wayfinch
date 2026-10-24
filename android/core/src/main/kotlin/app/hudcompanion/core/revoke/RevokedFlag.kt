// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.core.revoke

/**
 * The revoked flag (step 0 of the revocation sequence). Identities are the SPKI SHA-256 of the device certificate
 * in lower-case hex. There is no `clear()`: the flag ends only through [clearAfterNewEnrolment].
 */
public interface RevokedFlag {
    /** Whether a revocation was recorded. Throws when the flag cannot be read; the caller must then not connect. */
    public fun isSet(): Boolean

    /**
     * Records the revocation of [revokedIdentity] (any text: a malformed identity must still set the flag). The first
     * well-formed identity stays: a stored SPKI SHA-256 (in any case) is never replaced, a malformed stored text is
     * replaced by the next set, so a re-run of step 0 repairs a record left by a failed identity read.
     */
    public fun set(revokedIdentity: String)

    /**
     * Ends the revoked state after a successful enrolment as [newIdentity]. Refused (returns false, flag unchanged)
     * when [newIdentity] is not a SPKI SHA-256 or is the revoked identity itself. An unset flag returns true.
     */
    public fun clearAfterNewEnrolment(newIdentity: String): Boolean
}

/** Where the flag lives (an app preference in 7c): the revoked identity, or null when nothing is revoked. */
public interface RevokedFlagStorage {
    public fun load(): String?

    public fun save(revokedIdentity: String?)
}

private val spkiSha256 = Regex("[0-9a-f]{64}")

/**
 * [RevokedFlag] over a [RevokedFlagStorage]; every rule above lives here, the storage only reads and writes. The
 * methods are serialised per instance only, so the app holds exactly one instance per storage.
 */
public class PersistedRevokedFlag(
    private val storage: RevokedFlagStorage,
) : RevokedFlag {
    @Synchronized
    override fun isSet(): Boolean = storage.load() != null

    /** The first well-formed revoked identity wins: a re-run of step 0 replaces only a malformed stored text. */
    @Synchronized
    override fun set(revokedIdentity: String) {
        val stored = storage.load()
        if (stored == null || !spkiSha256.matches(stored.lowercase())) storage.save(revokedIdentity)
    }

    @Synchronized
    override fun clearAfterNewEnrolment(newIdentity: String): Boolean {
        val revoked = storage.load() ?: return true
        val allowed = spkiSha256.matches(newIdentity) && newIdentity != revoked.lowercase()
        if (allowed) storage.save(null)
        return allowed
    }
}
