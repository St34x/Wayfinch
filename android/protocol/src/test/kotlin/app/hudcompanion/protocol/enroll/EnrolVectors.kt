// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import org.junit.jupiter.api.Assertions.fail
import java.security.MessageDigest
import java.util.Base64

/** Fixed vectors of the T1.23c tests. */
internal object EnrolVectors {
    /**
     * The key-fingerprint test vector shared with the signer side: the SPKI DER of the P-256 generator
     * point, the public key of private scalar 1, 91 bytes. Test only, never enrolled; the signer side uses
     * it too.
     */
    const val SPKI_BASE64: String =
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaxfR8uEsQkf4vOblY6RA8ncDfYEt6zOg9KE5RdiYwpZP40Li/hp/m47n60p8D54WK84zV2sx" +
            "Xs7LtkBoN79R9Q=="

    /** `spki_sha256` of [SPKI_BASE64]: the SHA-256 of its DER bytes as lower-case hex. */
    const val SPKI_SHA256: String = "5cd252fb0ce8932436faf8ccd1040981b89ee4ad6b9fe9e2a2b7e71aacb27cd3"

    /** The display form of [SPKI_SHA256] (`keyFingerprint`): its first 16 hex digits, upper case, in groups of four. */
    const val FINGERPRINT: String = "5CD2 52FB 0CE8 9324"

    /**
     * The same key with the outer SEQUENCE length written in non-minimal long form (`30 81 59`): a decoder that
     * re-encodes the key changes these bytes (or refuses them), so a fingerprint over a re-encoding differs.
     */
    const val SPKI_LONG_FORM_BASE64: String =
        "MIFZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABGsX0fLhLEJH+Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT+NC4v4af5uO5+tKfA+eFivOM1dr" +
            "MV7Oy7ZAaDe/UfU="

    /** SHA-256 of [SPKI_LONG_FORM_BASE64]'s bytes (85d37d29492aca13...), as displayed. */
    const val LONG_FORM_FINGERPRINT: String = "85D3 7D29 492A CA13"

    fun spki(): ByteArray = Base64.getDecoder().decode(SPKI_BASE64)

    /** The proof bytes of a code that has the right shape; a test failure when [proofBytes] says null. */
    fun proof(
        code: String,
        spkiDer: ByteArray,
    ): ByteArray = proofBytes(code, spkiDer) ?: fail("no proof bytes for a well-formed code")

    fun hex(bytes: ByteArray): String = bytes.joinToString("") { (it.toInt() and 0xFF).toString(16).padStart(2, '0') }

    fun sha256Hex(bytes: ByteArray): String = hex(MessageDigest.getInstance("SHA-256").digest(bytes))
}
