# SPDX-License-Identifier: Apache-2.0
"""Language-neutral type model of a JSON Schema 2020-12 subset for the code emitters (ENGINEERING.md section 4). Stdlib.

Every keyword is either type-shaping (turned into the model: type, properties, required, items, enum, const, local
$ref) or narrowing-only: bounds, patterns and the conditional applicators (if/then/else/allOf/oneOf/not). Narrowing
keywords only remove instances, so the generated types are a sound superset; the schema validator and the codec
enforce the rest. Conditional subschemas are still checked: known keywords, declared property names only. Anything
else raises CodegenError naming the JSON pointer of the offending keyword.
"""

from __future__ import annotations  # ListOf refers to TypeExpr; keeps the tool runnable on Python 3.11-3.13

import keyword
import re
from dataclasses import dataclass
from typing import Literal

ANNOTATIONS = frozenset({"description", "$comment", "title", "default", "pattern", "dependentRequired"})
_BOUNDS = frozenset({"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "uniqueItems"})
NARROWING = ANNOTATIONS | _BOUNDS | frozenset({"minProperties", "maxProperties"})
CONDITIONAL = frozenset({"if", "then", "else", "allOf", "oneOf", "not"})
SHAPING = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum", "const", "$ref"})
SCALARS = {"string": "str", "integer": "int", "boolean": "bool"}
DEF_REF = re.compile(r"#/\$defs/(?P<name>[^/]+)")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

Json = dict[str, object]
LiteralValue = str | int | bool
Direction = Literal["D→S", "S→D", "both"]
SeqRule = Literal["required", "absent", "optional"]


class CodegenError(Exception):
    """The schema uses something the generator cannot represent; the message starts with the JSON pointer."""


@dataclass(frozen=True)
class Scalar:
    name: str  # "str" | "int" | "bool"


@dataclass(frozen=True)
class Literals:
    values: tuple[LiteralValue, ...]


@dataclass(frozen=True)
class Named:
    name: str  # an Alias or a Struct of the model


@dataclass(frozen=True)
class ListOf:
    item: TypeExpr


@dataclass(frozen=True)
class OpenObject:
    """An object without declared properties (skill args/output): any JSON object."""


TypeExpr = Scalar | Literals | Named | ListOf | OpenObject


@dataclass(frozen=True)
class Field:
    name: str
    type: TypeExpr
    required: bool


@dataclass(frozen=True)
class Struct:
    name: str
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class Alias:
    name: str
    type: TypeExpr


@dataclass(frozen=True)
class Frame:
    type_name: str
    body: str
    direction: Direction
    seq: SeqRule


@dataclass(frozen=True)
class ProtocolModel:
    frame_types: tuple[str, ...]
    aliases: tuple[Alias, ...]
    structs: tuple[Struct, ...]  # dependency order: a struct follows every struct it names
    header: tuple[Field, ...]  # envelope fields shared by every frame (not type, seq, body)
    seq_type: TypeExpr
    frames: tuple[Frame, ...]


def pascal(name: str) -> str:
    """`msg.out` -> `MsgOut`, `opaqueId` -> `OpaqueId`."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", name) if part)


def as_object(value: object, path: str) -> Json:
    if not isinstance(value, dict):
        raise CodegenError(f"{path}: expected an object")
    return value


def as_list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise CodegenError(f"{path}: expected an array")
    return value


def as_names(value: object, path: str) -> list[str]:
    items = as_list(value, path)
    if not all(isinstance(item, str) for item in items):
        raise CodegenError(f"{path}: expected an array of strings")
    return [str(item) for item in items]


def only_keys(node: Json, path: str, allowed: frozenset[str]) -> None:
    for key in node:
        if key not in allowed:
            raise CodegenError(f"{path}/{key}: unsupported keyword {key!r}")


def _literal(value: object, path: str) -> LiteralValue:
    if not isinstance(value, (str, int, bool)):
        raise CodegenError(f"{path}: only string, integer and boolean literals are supported")
    return value


def _declared(names: list[str], path: str, declared: frozenset[str]) -> None:
    for name in names:
        if name not in declared:
            raise CodegenError(f"{path}: {name!r} is not a declared property")


class TypeBuilder:
    def __init__(self, defs: Json) -> None:
        self.defs = defs
        self.structs: list[Struct] = []

    def type_of(self, node: Json, path: str, hint: str) -> TypeExpr:
        only_keys(node, path, SHAPING | NARROWING | CONDITIONAL)
        kind = node.get("type")
        if kind != "object":
            self.check_narrowing(node, path, frozenset())
        if "$ref" in node:
            return self._ref(node["$ref"], f"{path}/$ref")
        if "const" in node:
            return Literals((_literal(node["const"], f"{path}/const"),))
        if "enum" in node:
            return Literals(tuple(_literal(v, f"{path}/enum") for v in as_list(node["enum"], f"{path}/enum")))
        if kind == "object":
            return self.struct(node, path, hint)
        if kind == "array":
            return ListOf(self.type_of(as_object(node.get("items"), f"{path}/items"), f"{path}/items", hint + "Item"))
        if isinstance(kind, str) and kind in SCALARS:
            return Scalar(SCALARS[kind])
        raise CodegenError(f"{path}/type: unsupported or missing type {kind!r}")

    def _ref(self, ref: object, path: str) -> Named:
        match = DEF_REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is None or match["name"] not in self.defs:
            raise CodegenError(f"{path}: only refs to an existing #/$defs/<name> are supported, got {ref!r}")
        if match["name"].startswith("body."):
            raise CodegenError(f"{path}: body definitions are referenced only by the envelope dispatch")
        return Named(pascal(match["name"]))

    def struct(self, node: Json, path: str, name: str) -> TypeExpr:
        if "properties" not in node:
            only_keys(node, path, frozenset({"type"}) | NARROWING)
            return OpenObject()
        if node.get("additionalProperties") is not False:
            raise CodegenError(f"{path}/additionalProperties: an object with properties must set it to false")
        props = as_object(node["properties"], f"{path}/properties")
        required = as_names(node.get("required", []), f"{path}/required")
        _declared(required, f"{path}/required", frozenset(props))
        self.check_narrowing(node, path, frozenset(props))
        fields = tuple(
            Field(key, self.field_type(key, props[key], f"{path}/properties/{key}", name), key in required)
            for key in props
        )
        if any(existing.name == name for existing in self.structs):
            raise CodegenError(f"{path}: generated name {name!r} is used twice")
        self.structs.append(Struct(name, fields))
        return Named(name)

    def field_type(self, key: str, node: object, path: str, owner: str) -> TypeExpr:
        """Type of property `key` of struct `owner`; a nested struct is named `<owner without Body><Key>`."""
        if not IDENTIFIER.fullmatch(key) or keyword.iskeyword(key):
            raise CodegenError(f"{path}: property name {key!r} is not an identifier")
        return self.type_of(as_object(node, path), path, owner.removesuffix("Body") + pascal(key))

    def check_narrowing(self, node: Json, path: str, declared: frozenset[str]) -> None:
        """Conditional subschemas may only narrow: known keywords, declared property names."""
        for key in CONDITIONAL & node.keys():
            sub = f"{path}/{key}"
            items = as_list(node[key], sub) if key in ("allOf", "oneOf") else [node[key]]
            for index, item in enumerate(items):
                item_path = f"{sub}/{index}" if key in ("allOf", "oneOf") else sub
                self._narrowing_node(as_object(item, item_path), item_path, declared)

    def _narrowing_node(self, node: Json, path: str, declared: frozenset[str]) -> None:
        only_keys(node, path, CONDITIONAL | NARROWING | frozenset({"type", "enum", "const", "required", "properties"}))
        _declared(as_names(node.get("required", []), f"{path}/required"), f"{path}/required", declared)
        props = as_object(node.get("properties", {}), f"{path}/properties")
        _declared(list(props), f"{path}/properties", declared)
        for key, sub in props.items():
            self._narrowing_node(as_object(sub, f"{path}/properties/{key}"), f"{path}/properties/{key}", frozenset())
        self.check_narrowing(node, path, declared)
