# SPDX-License-Identifier: Apache-2.0
"""Kotlin text of a ProtocolModel (ENGINEERING.md section 4). Pure, stdlib.

One file, `Envelope.kt`: `sealed interface Body`, one `@Serializable @SerialName("<type>")` class per frame body (a
`data object` for an empty body), nested structs as `@Serializable data class`, `val` only, an optional field as a
nullable with a `null` default, a multi-valued enum as a Kotlin `enum class` (one `@SerialName` per entry), a
single-valued `const` as its primitive, every integer as `Long` carrying its schema bounds as `@WireInteger`, an open
object as `JsonObject`. `Envelope` (the header plus a `Body`) has a generated serializer: the wire form carries `type`
beside `body`, so the body class is picked by the `type` string.

The decoder is strict (design: kotlin_runtime.py docstring): `decodeEnvelope(text)` scans the raw text for duplicate
keys (kotlinx merges them, no JsonElement shows them), then walks the parsed tree with the serial descriptor and refuses
`null`, anything but a JSON integer token inside the bounds of an integer property, and a quoted boolean; the typed
decode refuses the rest. The Python codec refuses the same cases (fixtures: protocol/fixtures, both stacks run them).
"""

import re
from dataclasses import dataclass, field

from kotlin_runtime import HEADER, JSON, LIBRARY_NAMES, SERIALIZER, STRICT, SUPPORT_NAMES
from protocol_model import (
    Alias,
    CodegenError,
    Field,
    Frame,
    ListOf,
    Literals,
    LiteralValue,
    Named,
    OpenObject,
    ProtocolModel,
    Scalar,
    Struct,
    TypeExpr,
    pascal,
)

PACKAGE = "app.hudcompanion.protocol.generated"
SCALARS = {"str": "String", "int": "Long", "bool": "Boolean"}
HARD_KEYWORDS = frozenset({
    *("as", "break", "class", "continue", "do", "else", "false", "for", "fun", "if", "in", "interface", "is"),
    *("null", "object", "package", "return", "super", "this", "throw", "true", "try", "typealias", "typeof"),
    *("val", "var", "when", "while"),
})
IMPORTED = re.compile(r"^import [\w.]+\.([A-Z]\w*)$", re.MULTILINE)
RESERVED_TYPES = (*SUPPORT_NAMES, *LIBRARY_NAMES, *IMPORTED.findall(HEADER))
RESERVED_FIELDS = frozenset({"frameType"})
Bounds = tuple[int | None, int | None]


@dataclass
class _Names:
    """Type names in use (a clash is an error), the enums found while walking the fields, the aliases by name."""

    taken: set[str] = field(default_factory=set)
    enums: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    aliases: dict[str, TypeExpr] = field(default_factory=dict)

    def claim(self, name: str) -> str:
        if name in self.taken:
            raise CodegenError(f"generated Kotlin name {name!r} is used twice")
        self.taken.add(name)
        return name

    def bounds(self, expr: TypeExpr, seen: frozenset[str] = frozenset()) -> Bounds | None:
        """Schema bounds of the integer(s) `expr` stands for (a `const` is both bounds); None if it is no integer."""
        if isinstance(expr, Scalar):
            return (expr.minimum, expr.maximum) if expr.name == "int" else None
        if isinstance(expr, Literals) and len(expr.values) == 1:
            value = expr.values[0]
            return (value, value) if isinstance(value, int) and not isinstance(value, bool) else None
        if isinstance(expr, ListOf):
            return self.bounds(expr.item, seen)
        if isinstance(expr, Named) and expr.name in self.aliases and expr.name not in seen:
            return self.bounds(self.aliases[expr.name], seen | {expr.name})
        return None


@dataclass(frozen=True)
class _Param:
    name: str
    type: str
    required: bool
    bounds: Bounds | None = None

    @property
    def ident(self) -> str:
        return f"`{self.name}`" if self.name in HARD_KEYWORDS else self.name

    def declaration(self, visibility: str = "", *, annotated: bool = True) -> str:
        note = self._annotation() if annotated else ""
        return f"{note}    {visibility}val {self.ident}: {self.type}{'' if self.required else '? = null'},\n"

    def _annotation(self) -> str:
        """`@WireInteger(minimum = .., maximum = ..)` for an integer property (only the bounds the schema gives)."""
        if self.bounds is None:
            return ""
        given = zip(("minimum", "maximum"), self.bounds, strict=True)
        args = ", ".join(f"{key} = {value}" for key, value in given if value is not None)
        return "    @WireInteger" + (f"({args})" if args else "") + "\n"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _primitive(value: LiteralValue) -> str:
    return SCALARS["bool" if isinstance(value, bool) else "int" if isinstance(value, int) else "str"]


def _enum_values(expr: Literals, hint: str) -> tuple[str, ...]:
    values = tuple(value for value in expr.values if isinstance(value, str))
    if len(values) != len(expr.values):
        raise CodegenError(f"{hint}: an enum of several values must hold strings only")
    return values


def kotlin_type(expr: TypeExpr, hint: str, names: _Names) -> str:
    """Kotlin type of a model type expression; `hint` names the enum a multi-valued literal turns into."""
    if isinstance(expr, Scalar):
        return SCALARS[expr.name]
    if isinstance(expr, Literals) and len(expr.values) == 1:
        return _primitive(expr.values[0])
    if isinstance(expr, Literals):
        names.enums.append((names.claim(hint), _enum_values(expr, hint)))
        return hint
    if isinstance(expr, Named):
        return expr.name
    if isinstance(expr, ListOf):
        return f"List<{kotlin_type(expr.item, hint + 'Item', names)}>"
    if isinstance(expr, OpenObject):
        return "JsonObject"
    raise TypeError(expr)


def _param(item: Field, owner: str, names: _Names) -> _Param:
    if item.name in RESERVED_FIELDS:
        raise CodegenError(f"{owner}: property name {item.name!r} is reserved by the generated Body interface")
    kind = kotlin_type(item.type, owner + pascal(item.name), names)
    return _Param(item.name, kind, item.required, names.bounds(item.type))


def _struct(struct: Struct, frame: Frame | None, names: _Names) -> str:
    owner = struct.name.removesuffix("Body")
    head = "\n@Serializable\n" + (f"@SerialName({_quote(frame.type_name)})\n" if frame else "")
    super_type = ""
    if frame:
        super_type = f" : Body {{\n    override val frameType: String\n        get() = {_quote(frame.type_name)}\n}}"
    if not struct.fields:
        return f"{head}public data object {struct.name}{super_type}\n"
    params = "".join(_param(item, owner, names).declaration("public ") for item in struct.fields)
    return f"{head}public data class {struct.name}(\n{params}){super_type}\n"


def _entry(value: str, taken: set[str]) -> str:
    entry = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()
    if not entry or not entry[0].isalpha() or entry in taken:
        raise CodegenError(f"enum value {value!r} has no unique Kotlin entry name")
    taken.add(entry)
    return entry


def _enum(name: str, values: tuple[str, ...]) -> str:
    taken: set[str] = set()
    entries = "".join(f"    @SerialName({_quote(value)})\n    {_entry(value, taken)},\n" for value in values)
    return f"\n@Serializable\npublic enum class {name} {{\n{entries}}}\n"


def _alias(alias: Alias, names: _Names) -> str:
    if isinstance(alias.type, Literals) and len(alias.type.values) > 1:
        names.enums.append((alias.name, _enum_values(alias.type, alias.name)))
        return ""
    return f"\npublic typealias {alias.name} = {kotlin_type(alias.type, alias.name, names)}\n"


def _envelope(header: list[_Param]) -> str:
    params = "".join(item.declaration("public ", annotated=False) for item in header)
    return f"\n@Serializable(with = EnvelopeSerializer::class)\npublic data class Envelope(\n{params})\n"


def _set(name: str, types: list[str]) -> str:
    items = "".join(f"    {_quote(value)},\n" for value in types)
    return f"\npublic val {name}: Set<String> = setOf(\n{items})\n"


def _serializer(model: ProtocolModel, header: list[_Param]) -> str:
    """The wire class (header fields, `type` before `seq`, `body` as JsonObject), the type map and the codec."""
    fields = header[:-1]  # every envelope field but the body
    wire = [*fields[:-1], _Param("type", "String", True), fields[-1], _Param("body", "JsonObject", True)]
    text = SERIALIZER.replace("{wire_params}", "".join(item.declaration() for item in wire))
    text = text.replace("{decode_args}", "".join(f"            {n.ident} = wire.{n.ident},\n" for n in fields))
    text = text.replace("{encode_args}", "".join(f"            {n.ident} = value.{n.ident},\n" for n in fields))
    entries = "".join(f"    {_quote(f.type_name)} to {f.body}.serializer(),\n" for f in model.frames)
    return f"\nprivate val bodySerializers: Map<String, KSerializer<out Body>> = mapOf(\n{entries})\n" + text


def emit_kotlin(model: ProtocolModel) -> str:
    """The complete generated Envelope.kt text (raises CodegenError)."""
    names = _Names(aliases={alias.name: alias.type for alias in model.aliases})
    for name in (*RESERVED_TYPES, *(a.name for a in model.aliases), *(s.name for s in model.structs)):
        names.claim(name)
    frames = {frame.body: frame for frame in model.frames}
    header = [_param(item, "Envelope", names) for item in model.header]
    seq = kotlin_type(model.seq_type, "EnvelopeSeq", names)
    header.append(_Param("seq", seq, False, names.bounds(model.seq_type)))
    header.append(_Param("body", "Body", True))
    parts = [HEADER, _envelope(header)]
    parts += [_alias(alias, names) for alias in model.aliases]
    parts += [_struct(struct, frames.get(struct.name), names) for struct in model.structs]
    parts += [_enum(name, values) for name, values in names.enums]
    parts += [JSON, _serializer(model, header), STRICT]
    parts.append(_set("deviceToServerTypes", [f.type_name for f in model.frames if f.direction != "S→D"]))
    parts.append(_set("serverToDeviceTypes", [f.type_name for f in model.frames if f.direction != "D→S"]))
    return "".join(parts).replace("{package}", PACKAGE)
