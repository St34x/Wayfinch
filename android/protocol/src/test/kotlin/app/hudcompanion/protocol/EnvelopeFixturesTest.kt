// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol

import app.hudcompanion.protocol.generated.AckBody
import app.hudcompanion.protocol.generated.CardStateBody
import app.hudcompanion.protocol.generated.CardStateState
import app.hudcompanion.protocol.generated.ClarifyResponseBody
import app.hudcompanion.protocol.generated.Envelope
import app.hudcompanion.protocol.generated.HelloBody
import app.hudcompanion.protocol.generated.MsgOutBody
import app.hudcompanion.protocol.generated.MsgOutKind
import app.hudcompanion.protocol.generated.MsgOutNoticeKind
import app.hudcompanion.protocol.generated.PingBody
import app.hudcompanion.protocol.generated.WelcomeBody
import app.hudcompanion.protocol.generated.decodeEnvelope
import app.hudcompanion.protocol.generated.deviceToServerTypes
import app.hudcompanion.protocol.generated.encodeEnvelope
import app.hudcompanion.protocol.generated.envelopeJson
import app.hudcompanion.protocol.generated.serverToDeviceTypes
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertInstanceOf
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable
import java.io.File

/** The golden frames of protocol/fixtures decode into the generated types (T1.105); no hand-written envelope model. */
class EnvelopeFixturesTest {
    private val fixtures = File(requireNotNull(System.getProperty("protocol.fixtures")) { "protocol.fixtures unset" })

    private fun frames(dir: String): List<File> =
        fixtures
            .resolve(dir)
            .listFiles { file -> file.extension == "json" }
            .orEmpty()
            .sortedBy { it.name }

    private fun decode(file: File): Envelope = decodeEnvelope(file.readText())

    private fun fixture(
        dir: String,
        name: String,
    ): Envelope = decode(fixtures.resolve(dir).resolve(name))

    @Test
    fun `every valid and codec-valid fixture decodes and encodes back to the same JSON`() {
        val files = frames("valid") + frames("codec-valid")
        assertTrue(files.size >= 34 + 12, "fixtures found: ${files.size}")
        for (file in files) {
            val original = Json.parseToJsonElement(file.readText())
            val frame = decode(file)
            assertEquals(original, envelopeJson.encodeToJsonElement(Envelope.serializer(), frame), file.name)
            assertEquals(original, Json.parseToJsonElement(encodeEnvelope(frame)), file.name)
        }
    }

    @Test
    fun `the valid fixtures cover every frame type of the contract`() {
        val seen = frames("valid").map { decode(it).body.frameType }.toSet()
        assertEquals(deviceToServerTypes + serverToDeviceTypes, seen)
        assertEquals(27, seen.size)
    }

    @Test
    fun `direction sets follow the message type table`() {
        assertTrue(setOf("hello", "msg.in", "ping", "pong", "error").all { it in deviceToServerTypes })
        assertTrue(setOf("welcome", "msg.out", "ping", "pong", "error").all { it in serverToDeviceTypes })
        assertTrue(setOf("welcome", "msg.out").none { it in deviceToServerTypes })
        assertTrue(setOf("hello", "msg.in").none { it in serverToDeviceTypes })
    }

    @Test
    fun `fields land in typed properties`() {
        val notice = fixture("valid", "msg.out-notice.json")
        assertEquals(7L, notice.seq)
        val out = assertInstanceOf(MsgOutBody::class.java, notice.body)
        assertEquals(MsgOutKind.NOTICE, out.kind)
        assertEquals(MsgOutNoticeKind.KANBAN_DONE, out.notice?.kind)
        assertEquals("t-42", out.notice?.ref)
        assertNull(out.longRef)
        assertInstanceOf(HelloBody::class.java, fixture("valid", "hello.json").body)
        assertInstanceOf(WelcomeBody::class.java, fixture("valid", "welcome.json").body)
        assertInstanceOf(PingBody::class.java, fixture("valid", "ping.json").body)
        val card =
            assertInstanceOf(CardStateBody::class.java, fixture("valid", "card.state-clarify-detached.json").body)
        assertEquals(CardStateState.DETACHED, card.state)
        val dismissFrame = fixture("valid", "clarify.response-dismiss.json")
        val dismiss = assertInstanceOf(ClarifyResponseBody::class.java, dismissFrame.body)
        assertEquals(true, dismiss.dismiss)
        assertNull(dismiss.answer)
    }

    @Test
    fun `absent optional fields are left out when encoding`() {
        val frame = Envelope(v = 1, id = "01JABCDEFGHJKMNPQRSTVWXYZ0", ts = "2026-10-03T18:00:00Z", body = AckBody(5))
        val json = envelopeJson.encodeToJsonElement(Envelope.serializer(), frame).jsonObject
        assertEquals(setOf("v", "id", "ts", "type", "body"), json.keys)
        assertEquals("ack", json.getValue("type").jsonPrimitive.content)
        assertEquals(setOf("upToSeq"), json.getValue("body").jsonObject.keys)
        val ping = envelopeJson.encodeToJsonElement(Envelope.serializer(), frame.copy(body = PingBody)).jsonObject
        assertEquals(JsonObject(emptyMap()), ping.getValue("body"))
    }

    @Test
    fun `encoding ignores the callers Json settings`() {
        val frame = Envelope(v = 1, id = "01JABCDEFGHJKMNPQRSTVWXYZ0", ts = "2026-10-03T18:00:00Z", body = AckBody(5))
        val loose =
            Json {
                encodeDefaults = true
                ignoreUnknownKeys = true
            }
        val json = loose.encodeToJsonElement(Envelope.serializer(), frame).jsonObject
        assertEquals(setOf("v", "id", "ts", "type", "body"), json.keys)
        assertEquals(json, Json.parseToJsonElement(encodeEnvelope(frame)))
    }

    @Test
    fun `only decodeEnvelope decodes a frame`() {
        val text = fixtures.resolve("valid").resolve("ack.json").readText()
        val lax = Json { ignoreUnknownKeys = true }
        for (json in listOf(envelopeJson, Json, lax)) {
            val failing = Executable { json.decodeFromString(Envelope.serializer(), text) }
            val refused = assertThrows(SerializationException::class.java, failing)
            assertTrue(refused.message.orEmpty().contains("decodeEnvelope"), refused.message)
        }
    }
}
