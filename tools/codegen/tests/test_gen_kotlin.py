# SPDX-License-Identifier: Apache-2.0
"""Tests of tools/codegen (T1.105): the Kotlin text of the real protocol model and of small hand-built models."""

import json
import re
from pathlib import Path

import gen_kotlin
import pytest
from gen_protocol import load_model
from kotlin_emitter import RESERVED_TYPES, emit_kotlin
from kotlin_runtime import HEADER, JSON, SERIALIZER, STRICT
from protocol_model import (
    Alias,
    CodegenError,
    Field,
    Frame,
    ListOf,
    Literals,
    Named,
    OpenObject,
    ProtocolModel,
    Scalar,
    Struct,
    pascal,
)

# tools/codegen is on sys.path through conftest.py.
ROOT = Path(__file__).resolve().parents[3]
MODEL = load_model(ROOT)
TEXT = emit_kotlin(MODEL)
COMMITTED = ROOT / gen_kotlin.OUTPUT


def _model(
    fields: tuple[Field, ...] = (),
    aliases: tuple[Alias, ...] = (),
    extra: tuple[Struct, ...] = (),
    seq: str = "optional",
) -> ProtocolModel:
    """One frame `probe.type` whose body is `ProbeBody(fields)`, after any extra structs."""
    frame = Frame("probe.type", "ProbeBody", "both", seq)  # type: ignore[arg-type]
    header = (Field("v", Literals((1,)), True), Field("id", Named("Ulid"), True))
    return ProtocolModel(
        ("probe.type",), aliases, (*extra, Struct("ProbeBody", fields)), header, Scalar("int"), (frame,)
    )


def _error(model: ProtocolModel) -> str:
    with pytest.raises(CodegenError) as caught:
        emit_kotlin(model)
    return str(caught.value)


def test_committed_file_is_the_generated_text() -> None:
    assert COMMITTED.read_text(encoding="utf-8") == TEXT


def test_output_is_deterministic() -> None:
    assert emit_kotlin(load_model(ROOT)) == TEXT


def test_marker_and_spdx_are_in_the_first_lines() -> None:
    first = TEXT.splitlines()[:10]
    assert first[0] == "// SPDX-License-Identifier: Apache-2.0"
    assert any(re.fullmatch(r"//\s*@generated\s.*", line) for line in first)
    assert TEXT.endswith("\n") and not TEXT.endswith("\n\n")


def test_every_frame_type_has_one_serial_name_class_with_its_frame_type() -> None:
    assert len(MODEL.frames) == 27
    for frame in MODEL.frames:
        pattern = (
            rf'@Serializable\n@SerialName\("{re.escape(frame.type_name)}"\)\n'
            rf"public data (?:class|object) {frame.body}\b.*?: Body \{{\n"
            rf'    override val frameType: String\n        get\(\) = "{re.escape(frame.type_name)}"\n\}}'
        )
        assert len(re.findall(pattern, TEXT, re.DOTALL)) == 1, frame.type_name
    assert len(re.findall(r"^public data (?:class|object) \w+Body\b", TEXT, re.MULTILINE)) == 27


def test_empty_bodies_are_data_objects() -> None:
    assert "public data object PingBody : Body {" in TEXT
    assert "public data object PongBody : Body {" in TEXT


def test_every_body_is_registered_once_in_the_type_map() -> None:
    entries = re.findall(r'^    "([^"]+)" to (\w+)\.serializer\(\),$', TEXT, re.MULTILINE)
    assert entries == [(frame.type_name, frame.body) for frame in MODEL.frames]


def test_optional_fields_are_nullable_with_a_null_default_and_required_are_not() -> None:
    assert "    public val langHint: LangTag? = null,\n" in TEXT
    assert "    public val text: String,\n" in TEXT
    assert "    public val notice: MsgOutNotice? = null,\n" in TEXT
    assert "    public val lastAckedSeq: Long,\n" in TEXT


def test_no_mutable_property_or_collection() -> None:
    types = TEXT.split("public val envelopeJson")[0]  # the data model; the support code below uses local variables
    assert not re.search(r"\b(?:var|lateinit|MutableList|MutableMap|MutableSet|Array)\b", types)


def test_enum_entries_carry_the_wire_value() -> None:
    assert '@SerialName("answered_elsewhere")\n    ANSWERED_ELSEWHERE,\n' in TEXT
    assert "public enum class MsgOutKind {\n" in TEXT
    assert '    @SerialName("TERM")\n    TERM,\n' in TEXT
    assert "public enum class HintLevel {\n" in TEXT  # a $defs enum is the enum, not a typealias


def test_single_valued_constants_are_primitives() -> None:
    assert "    public val v: Long,\n" in TEXT
    assert "    public val mime: String,\n" in TEXT
    assert "    public val dismiss: Boolean? = null,\n" in TEXT


def test_open_objects_are_json_objects() -> None:
    assert "    public val args: JsonObject,\n" in TEXT
    assert "    public val output: JsonObject? = null,\n" in TEXT
    assert "    public val skills: List<JsonObject>,\n" in TEXT


def test_direction_sets_follow_the_message_type_table() -> None:
    device = re.search(r"deviceToServerTypes: Set<String> = setOf\(\n(.*?)\)\n", TEXT, re.DOTALL)
    server = re.search(r"serverToDeviceTypes: Set<String> = setOf\(\n(.*?)\)\n", TEXT, re.DOTALL)
    assert device is not None and server is not None
    to_server = set(re.findall(r'"([^"]+)"', device[1]))
    to_device = set(re.findall(r'"([^"]+)"', server[1]))
    assert {"hello", "msg.in", "ping", "pong", "error"} <= to_server
    assert {"welcome", "msg.out", "ping", "pong", "error"} <= to_device
    assert not {"welcome", "msg.out"} & to_server
    assert not {"hello", "msg.in"} & to_device


def test_no_other_product_or_vendor_name_in_the_generated_text() -> None:
    banned = ["".join(parts) for parts in (("ope", "nai"), ("anth", "ropic"), ("ever", "realities"))]
    assert not [word for word in banned if word in TEXT.lower()]


def test_small_model_shapes() -> None:
    items = Field("tags", ListOf(Literals(("a-b", "c/d"))), True)
    odd = Field("in", Scalar("str"), False)
    text = emit_kotlin(_model((items, odd, Field("blob", OpenObject(), True))))
    assert "    public val tags: List<ProbeTagsItem>,\n" in text
    assert "    public val `in`: String? = null,\n" in text
    assert '    @SerialName("a-b")\n    A_B,\n    @SerialName("c/d")\n    C_D,\n' in text
    assert "    public val blob: JsonObject,\n" in text
    wire_head = (
        "private class EnvelopeWire(\n    @WireInteger(minimum = 1, maximum = 1)\n    val v: Long,\n    val id: Ulid,\n"
        "    val type: String,\n    @WireInteger\n    val seq"
    )
    assert wire_head + ": Long? = null," in text


def test_small_model_aliases() -> None:
    aliases = (Alias("Ulid", Scalar("str")), Alias("Level", Literals(("low", "high"))))
    text = emit_kotlin(_model((Field("level", Named("Level"), True),), aliases))
    assert "public typealias Ulid = String\n" in text
    assert "public enum class Level {\n" in text
    assert "typealias Level" not in text
    assert "    public val level: Level,\n" in text


def test_enum_values_that_collapse_to_one_entry_are_an_error() -> None:
    message = _error(_model((Field("kind", Literals(("a-b", "a_b")), True),)))
    assert "'a_b'" in message and "unique Kotlin entry name" in message


def test_enum_value_without_a_letter_start_is_an_error() -> None:
    assert "'1st'" in _error(_model((Field("kind", Literals(("1st", "2nd")), True),)))


def test_mixed_enum_values_are_an_error() -> None:
    assert "ProbeKind" in _error(_model((Field("kind", Literals(("a", 2)), True),)))


def test_reserved_property_name_is_an_error() -> None:
    assert "frameType" in _error(_model((Field("frameType", Scalar("str"), True),)))


LIBRARY_AND_SUPPORT_NAMES = [
    *("Body", "Envelope", "EnvelopeSerializer", "EnvelopeWire", "WireInteger"),
    *("String", "Long", "Boolean", "List", "Set", "Map", "Regex", "Any", "Unit", "ArrayDeque", "HashSet"),
    *("Json", "JsonObject", "JsonArray", "JsonElement", "JsonNull", "JsonPrimitive", "JsonDecoder", "JsonEncoder"),
    *("KSerializer", "Serializable", "SerialName", "SerialInfo", "SerialDescriptor", "SerializationException"),
    *("Decoder", "Encoder", "PrimitiveKind", "StructureKind", "CompositeDecoder", "ExperimentalSerializationApi"),
]


@pytest.mark.parametrize("name", LIBRARY_AND_SUPPORT_NAMES)
def test_generated_name_clash_with_the_names_the_file_uses_is_an_error(name: str) -> None:
    assert name in _error(_model(extra=(Struct(name, (Field("x", Scalar("int"), True),)),)))


def test_a_definition_named_like_a_library_type_is_refused_before_it_can_shadow_it() -> None:
    for lower in ("string", "json", "list", "set"):  # pascal() turns the $defs name into String, Json, List, Set
        assert pascal(lower) in _error(_model(aliases=(Alias(pascal(lower), Scalar("str")),)))


def test_every_camel_case_name_of_the_support_text_is_reserved() -> None:
    used: set[str] = set()
    for text in (HEADER, JSON, SERIALIZER, STRICT):
        for line in text.splitlines():
            code = line.split("//")[0]
            if not code.strip().startswith(("/**", "*")):
                used |= set(re.findall(r"\b[A-Z][a-z][A-Za-z0-9]*\b", code))
    assert {"Json", "Regex", "SerialDescriptor", "EnvelopeWire"} <= used  # the scan sees the code
    assert used <= set(RESERVED_TYPES), sorted(used - set(RESERVED_TYPES))


def _int_properties(node: object) -> list[tuple[str, int | None, int | None]]:
    found: list[tuple[str, int | None, int | None]] = []
    if isinstance(node, dict):
        for name, spec in node.get("properties", {}).items():
            if isinstance(spec, dict) and spec.get("type") == "integer":
                found.append((name, spec.get("minimum"), spec.get("maximum")))
        for child in node.values():
            found += _int_properties(child)
    elif isinstance(node, list):
        for child in node:
            found += _int_properties(child)
    return found


def test_every_integer_property_of_the_schema_carries_its_bounds() -> None:
    schema = json.loads((ROOT / "protocol/envelope.schema.json").read_text(encoding="utf-8"))
    properties = _int_properties(schema)
    assert len(properties) >= 15
    for name, low, high in properties:
        given = (("minimum", low), ("maximum", high))
        note = "@WireInteger(" + ", ".join(f"{k} = {v}" for k, v in given if v is not None) + ")"
        assert re.search(rf"^    {re.escape(note)}\n    (?:public )?val {name}: Long", TEXT, re.MULTILINE), name


def test_a_const_integer_and_an_aliased_or_listed_integer_get_bounds() -> None:
    aliases = (Alias("Count", Scalar("int", 0, 9)),)
    fields = (Field("count", Named("Count"), True), Field("sizes", ListOf(Scalar("int", 1, None)), False))
    text = emit_kotlin(_model(fields, aliases))
    assert "    @WireInteger(minimum = 0, maximum = 9)\n    public val count: Count,\n" in text
    assert "    @WireInteger(minimum = 1)\n    public val sizes: List<Long>? = null,\n" in text
    assert "    @WireInteger(minimum = 1, maximum = 1)\n    val v: Long,\n" in text


def test_the_decoder_is_strict_and_has_one_public_entry() -> None:
    assert "public fun decodeEnvelope(text: String): Envelope {\n    scanRawText(text)\n" in TEXT
    assert "internal object EnvelopeSerializer" in TEXT and "public object EnvelopeSerializer" not in TEXT
    assert 'SerialDescriptor("app.hudcompanion.protocol.generated.Envelope", ' in TEXT  # the wire class stays private
    assert "input.json !== checkedJson" in TEXT
    assert "output.json" not in TEXT and "input.json.decode" not in TEXT  # the caller's Json settings are not used
    assert TEXT.count("requireStrict(") >= 3 and "integerToken" in TEXT and "is JsonNull" in TEXT


def test_the_raw_scan_holds_the_python_codec_limits_and_the_checked_json_stays_private() -> None:
    assert "private const val MAX_NESTING = 35" in TEXT and "private const val MAX_INTEGER_DIGITS = 4300" in TEXT
    assert "if (open.size > MAX_NESTING) throw notJson(" in TEXT
    assert "if (text[at] < ' ') throw notJson(\"control character in a string\")" in TEXT
    assert "private val checkedJson: Json" in TEXT and "Must stay private" in TEXT
    assert "val checkedJson" not in TEXT.replace("private val checkedJson", "")


def test_enum_named_like_a_struct_is_an_error() -> None:
    clash = Struct("ProbeKind", (Field("x", Scalar("int"), True),))
    assert "ProbeKind" in _error(_model((Field("kind", Literals(("a", "b")), True),), extra=(clash,)))
