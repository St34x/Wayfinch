// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File

/** The golden frames of protocol/fixtures (T1.03), read through the `protocol.fixtures` property of the build. */
internal val fixtureDir = File(requireNotNull(System.getProperty("protocol.fixtures")) { "protocol.fixtures unset" })

internal fun fixtureFile(
    dir: String,
    name: String,
): File = fixtureDir.resolve(dir).resolve("$name.json")

internal fun fixtureNames(dir: String): List<String> =
    fixtureDir
        .resolve(dir)
        .listFiles { file -> file.extension == "json" }
        .orEmpty()
        .map { it.nameWithoutExtension }
        .sorted()

/** The entries of one list of `index.json` (`raw-invalid`: file, dir, defect, rule, repair). */
internal fun indexEntries(kind: String): List<JsonObject> =
    Json
        .parseToJsonElement(fixtureDir.resolve("index.json").readText())
        .jsonObject
        .getValue(kind)
        .jsonArray
        .map { it.jsonObject }

/** The text of a `not-json` fixture with its one defect repaired (`index.json`: replace `find` by `replace`). */
internal fun repairedText(entry: JsonObject): String {
    val repair = entry.getValue("repair").jsonObject
    val find = repair.getValue("find").jsonPrimitive.content
    val text = fixtureDir.resolve("raw-invalid").resolve(entry.getValue("file").jsonPrimitive.content).readText()
    check(text.split(find).size == 2) { "repair text must occur once in ${entry["file"]}" }
    return text.replace(find, repair.getValue("replace").jsonPrimitive.content)
}

private const val INTEGER = "expected a JSON integer"
private const val BOUNDS = "integer outside its bounds"
private const val NULL = "null is not allowed"
private const val UNKNOWN_KEY = "unknown key"
private const val NESTING = "nesting deeper than 35 levels"

/** (directory, fixture, reason in the refusal): every fixture the typed model refuses, with the path of the field. */
internal val refusals =
    listOf(
        Triple("invalid", "extra-envelope-field", UNKNOWN_KEY),
        Triple("invalid", "extra-body-field", UNKNOWN_KEY),
        Triple("invalid", "ping-body-extra-key", UNKNOWN_KEY),
        Triple("invalid", "clarify-request-expires-at", UNKNOWN_KEY),
        Triple("invalid", "unknown-type", "unknown frame type"),
        Triple("invalid", "type-number", "quoted"),
        Triple("invalid", "body-array", "JsonObject"),
        Triple("invalid", "body-null", "frame.body: $NULL"),
        Triple("invalid", "corr-null", "frame.corr: $NULL"),
        Triple("invalid", "body-optional-null", "frame.body.langHint: $NULL"),
        Triple("invalid", "wrong-version-string", "frame.v: $INTEGER"),
        Triple("invalid", "wrong-version-2", "frame.v: $BOUNDS"),
        Triple("invalid", "seq-string", "frame.seq: $INTEGER"),
        Triple("invalid", "ack-up-to-seq-string", "frame.body.upToSeq: $INTEGER"),
        Triple("invalid", "ack-up-to-seq-negative", "frame.body.upToSeq: $BOUNDS"),
        Triple("invalid", "ack-up-to-seq-too-large", "frame.body.upToSeq: $BOUNDS"),
        Triple("invalid", "clarify-dismiss-string", "frame.body.dismiss: expected a JSON boolean"),
        Triple("invalid", "long-ref-truncated-string", "frame.body.longRef.truncated: expected a JSON boolean"),
        Triple("codec-invalid", "d2s-ack-up-to-seq-exponent", "frame.body.upToSeq: $INTEGER"),
        Triple("codec-invalid", "d2s-ack-up-to-seq-decimal", "frame.body.upToSeq: $INTEGER"),
        Triple("codec-invalid", "d2s-ack-version-decimal", "frame.v: $INTEGER"),
        Triple("codec-invalid", "s2d-msg-out-seq-exponent", "frame.seq: $INTEGER"),
        Triple("codec-invalid", "d2s-skill-manifest-item-depth-33", NESTING),
    )

/** A fixture that is refused with a reason of its own (the typed decode, not a message the table can pin). */
internal val refusedWithoutReason = setOf("invalid/missing-body")

/**
 * The fixtures the phone's decoder accepts although the schema or the Python codec refuses them, by the rule they
 * break (protocol/README.md, "Kotlin decoder"). A fixture leaves this list when the decoder starts to refuse it and
 * is added only with a reason; EnvelopeFixtureCoverageTest fails both ways.
 */
internal val knownGaps: Map<String, List<String>> =
    mapOf(
        "lane pattern" to
            listOf("lane-too-long", "lane-trailing-cr", "lane-trailing-lf", "lane-uppercase")
                .map { "invalid/$it" },
        "id pattern" to listOf("invalid/bad-ulid"),
        "base64 pattern" to
            listOf("bad-base64-alphabet", "bad-base64-length", "bad-base64-padding").map { "invalid/$it" },
        "timestamp pattern" to listOf("bad-ts-month", "bad-ts-no-offset").map { "invalid/$it" },
        "bidi control characters" to listOf("invalid/approval-summary-bidi"),
        "string length" to
            listOf("oversize-long-ref-text", "oversize-msg-in-text", "oversize-msg-out-text")
                .map { "invalid/$it" },
        "conditional keywords" to
            listOf("clarify-card-expired", "long-ref-truncated-without-full-text")
                .map { "invalid/$it" },
        "seq direction" to
            listOf(
                "invalid/missing-seq-on-msg-out",
                "invalid/seq-on-msg-in",
                "invalid/seq-on-ping",
                "invalid/seq-on-welcome",
                "codec-invalid/d2s-error-with-seq",
            ),
        "UTF-8 byte limit" to
            listOf(
                "d2s-msg-in-text-2002b-cyrillic",
                "s2d-msg-out-full-text-4098b-cyrillic",
                "s2d-msg-out-full-text-4100b-emoji",
                "s2d-msg-out-text-2050b-cyrillic",
                "s2d-msg-out-text-2052b-emoji",
                "s2d-msg-out-text-514b-long-ref-cyrillic",
                "s2d-msg-out-text-516b-long-ref-emoji",
            ).map { "codec-invalid/$it" },
        "calendar of ts" to listOf("codec-invalid/d2s-ts-2027-02-29"),
        "lone surrogate" to listOf("codec-invalid/d2s-msg-in-lone-surrogate"),
        "depth 32 of an open field" to
            listOf("d2s-skill-result-output-depth-33", "s2d-skill-call-args-depth-33")
                .map { "codec-invalid/$it" },
    )
