// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import java.security.MessageDigest

/** The one-time code of the enrolment payload and of the `/enroll` body: 22 characters of base64url. */
internal val enrolCodeShape: Regex = Regex("[A-Za-z0-9_-]{22}")

private const val PROOF_PREFIX = "wayfinch-enroll-v1\u0000"
private const val FINGERPRINT_BYTES = 8
private const val FINGERPRINT_GROUP = 4
private const val HEX_DIGITS = "0123456789ABCDEF"
private const val NIBBLE_BITS = 4
private const val NIBBLE_MASK = 0xF
private const val BYTE_MASK = 0xFF

/**
 * The bytes the device key signs to prove possession at enrolment (`protocol/README.md` "HTTP
 * `/enroll`"): `wayfinch-enroll-v1`, one NUL byte, the [code] as ASCII, then [spkiDer] unchanged. Null when [code] is
 * not 22 base64url characters, the same refusal [EnrolRequest.of] gives as `BAD_CODE`: nothing is thrown, nothing is
 * signed. The caller passes `EnrolmentPayload.Parsed.code`, which always has the shape.
 */
public fun proofBytes(
    code: String,
    spkiDer: ByteArray,
): ByteArray? =
    if (enrolCodeShape.matches(code)) (PROOF_PREFIX + code).toByteArray(Charsets.US_ASCII) + spkiDer else null

/**
 * The key fingerprint the phone shows beside the root-side confirmation card: the first 16 hex digits of the SHA-256 of
 * [spkiDer], upper case, in four groups of four (`5CD2 52FB 0CE8 9324`).
 */
public fun keyFingerprint(spkiDer: ByteArray): String {
    val digest = MessageDigest.getInstance("SHA-256").digest(spkiDer)
    val hex = StringBuilder(FINGERPRINT_BYTES * 2)
    for (byte in digest.copyOf(FINGERPRINT_BYTES)) {
        val value = byte.toInt() and BYTE_MASK
        hex.append(HEX_DIGITS[value shr NIBBLE_BITS]).append(HEX_DIGITS[value and NIBBLE_MASK])
    }
    return hex.chunked(FINGERPRINT_GROUP).joinToString(" ")
}
