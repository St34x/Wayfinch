# SPDX-License-Identifier: Apache-2.0
"""The envelope of protocol/envelope.schema.json as a ProtocolModel (ENGINEERING.md section 4). Pure, stdlib.

The envelope's `allOf` holds two kinds of `if`/`then` blocks keyed on `type`: the body dispatch (const X -> `body`
$ref `#/$defs/body.X`, exactly one per type, every body.* def bound) and the `seq` rule (required / forbidden for an
enum of types). The direction of each type comes from the "Message types" table of protocol/README.md (the schema
cannot see it) and must agree with the seq rule: S→D types require seq, D→S types forbid it.
"""

import re
from dataclasses import dataclass

from protocol_model import (
    ANNOTATIONS,
    NARROWING,
    Alias,
    CodegenError,
    Direction,
    Field,
    Frame,
    Json,
    Named,
    ProtocolModel,
    SeqRule,
    TypeBuilder,
    as_list,
    as_names,
    as_object,
    only_keys,
    pascal,
)

ROOT_KEYS = ANNOTATIONS | {"$schema", "$id", "type", "properties", "required", "additionalProperties", "allOf", "$defs"}
DIRECTION_ROW = re.compile(r"\|\s*`(?P<type>[^`]+)`\s*\|\s*(?P<dir>D→S|S→D|both)\s*\|")
DIRECTIONS: dict[str, Direction] = {"D→S": "D→S", "S→D": "S→D", "both": "both"}
TYPES_SECTION = "## Message types"


def parse_directions(readme: str, frame_types: tuple[str, ...]) -> dict[str, Direction]:
    """Direction of every frame type from the `| type | dir | ... |` table of the README's "Message types" section."""
    _, found_section, rest = readme.partition(f"\n{TYPES_SECTION}\n")
    section = rest.split("\n## ", 1)[0]
    found: dict[str, Direction] = {}
    for line in section.splitlines():
        match = DIRECTION_ROW.match(line.strip())
        if match is None:
            continue
        if match["type"] in found:
            raise CodegenError(f"protocol/README.md: type {match['type']!r} has two rows in {TYPES_SECTION!r}")
        found[match["type"]] = DIRECTIONS[match["dir"]]
    missing = [name for name in frame_types if name not in found]
    unknown = [name for name in found if name not in frame_types]
    if not found_section or missing or unknown:
        raise CodegenError(
            f"protocol/README.md: {TYPES_SECTION!r} table does not match the schema `type` enum "
            f"(missing {missing}, unknown {unknown})"
        )
    return found


@dataclass
class _Dispatch:
    bodies: dict[str, str]
    seq_required: set[str]
    seq_absent: set[str]


def _condition_types(cond: Json, path: str, frame_types: tuple[str, ...]) -> list[str]:
    """`if` of an envelope block: a const/enum test on `type` only."""
    props = as_object(cond.get("properties"), f"{path}/properties")
    test = as_object(props.get("type"), f"{path}/properties/type")
    if set(cond) != {"properties", "required"} or set(props) != {"type"} or cond["required"] != ["type"]:
        raise CodegenError(f"{path}: expected a test of `type` only (properties.type + required [type])")
    if set(test) not in ({"const"}, {"enum"}):
        raise CodegenError(f"{path}/properties/type: expected exactly one of const / enum")
    types = [test["const"]] if "const" in test else as_list(test["enum"], f"{path}/properties/type/enum")
    for name in types:
        if name not in frame_types:
            raise CodegenError(f"{path}/properties/type: {name!r} is not in the `type` enum")
    return [str(name) for name in types]


def _apply_then(dispatch: _Dispatch, types: list[str], then: Json, path: str) -> None:
    if then == {"required": ["seq"]}:
        dispatch.seq_required.update(types)
        return
    if then == {"not": {"required": ["seq"]}}:
        dispatch.seq_absent.update(types)
        return
    body = then.get("properties")
    expected = f"#/$defs/body.{types[0]}"
    if set(then) != {"properties"} or len(types) != 1 or body != {"body": {"$ref": expected}}:
        raise CodegenError(f"{path}: expected a seq rule or properties.body.$ref {expected!r}")
    if types[0] in dispatch.bodies:
        raise CodegenError(f"{path}: type {types[0]!r} has a second body definition")
    dispatch.bodies[types[0]] = f"body.{types[0]}"


def _read_dispatch(blocks: object, frame_types: tuple[str, ...], defs: Json) -> _Dispatch:
    dispatch = _Dispatch({}, set(), set())
    for index, block in enumerate(as_list(blocks, "#/allOf")):
        path = f"#/allOf/{index}"
        node = as_object(block, path)
        only_keys(node, path, frozenset({"if", "then"}))
        types = _condition_types(as_object(node.get("if"), f"{path}/if"), f"{path}/if", frame_types)
        _apply_then(dispatch, types, as_object(node.get("then"), f"{path}/then"), f"{path}/then")
    for name in frame_types:
        if name not in dispatch.bodies or f"body.{name}" not in defs:
            raise CodegenError(f"#/allOf: type {name!r} needs exactly one body block and #/$defs/body.{name}")
    unbound = [name for name in defs if name.startswith("body.") and name[5:] not in dispatch.bodies]
    if unbound:
        raise CodegenError(f"#/$defs: body definitions without a dispatch block: {unbound}")
    return dispatch


def _seq_rule(name: str, dispatch: _Dispatch, direction: Direction) -> SeqRule:
    if name in dispatch.seq_required and name in dispatch.seq_absent:
        raise CodegenError(f"#/allOf: type {name!r} both requires and forbids seq")
    rule: SeqRule = "optional"
    if name in dispatch.seq_required:
        rule = "required"
    elif name in dispatch.seq_absent:
        rule = "absent"
    if (direction == "S→D") != (rule == "required") or (direction == "D→S" and rule != "absent"):
        raise CodegenError(f"#/allOf: type {name!r} is {direction} in protocol/README.md but seq is {rule}")
    return rule


def _alias(builder: TypeBuilder, name: str, node: object) -> Alias:
    path = f"#/$defs/{name}"
    if as_object(node, path).get("type") == "object":
        raise CodegenError(f"{path}: object definitions are supported only as body.<type>")
    return Alias(pascal(name), builder.type_of(as_object(node, path), path, pascal(name)))


def _header(builder: TypeBuilder, schema: Json, props: Json) -> tuple[Field, ...]:
    if schema.get("additionalProperties") is not False:
        raise CodegenError("#/additionalProperties: the envelope must set it to false")
    body = as_object(props.get("body"), "#/properties/body")
    only_keys(body, "#/properties/body", frozenset({"type"}) | NARROWING)
    required = as_names(schema.get("required", []), "#/required")
    return tuple(
        Field(key, builder.field_type(key, props[key], f"#/properties/{key}", "Frame"), key in required)
        for key in props
        if key not in ("type", "seq", "body")
    )


def _body(builder: TypeBuilder, name: str, defs: Json) -> str:
    path, body = f"#/$defs/body.{name}", pascal(name) + "Body"
    if builder.type_of(as_object(defs[f"body.{name}"], path), path, body) != Named(body):
        raise CodegenError(f"{path}: a body must be an object with properties")
    return body


def build_model(schema: Json, readme: str) -> ProtocolModel:
    """The whole protocol: aliases, structs (dependency order), envelope header and one Frame per type."""
    only_keys(schema, "#", ROOT_KEYS)
    defs = as_object(schema.get("$defs"), "#/$defs")
    props = as_object(schema.get("properties"), "#/properties")
    frame_types = tuple(
        as_names(as_object(props.get("type"), "#/properties/type").get("enum"), "#/properties/type/enum")
    )
    dispatch = _read_dispatch(schema.get("allOf"), frame_types, defs)
    directions = parse_directions(readme, frame_types)
    builder = TypeBuilder(defs)
    aliases = tuple(_alias(builder, name, defs[name]) for name in defs if not name.startswith("body."))
    header = _header(builder, schema, props)
    seq_type = builder.type_of(as_object(props.get("seq"), "#/properties/seq"), "#/properties/seq", "Seq")
    frames = tuple(
        Frame(name, _body(builder, name, defs), directions[name], _seq_rule(name, dispatch, directions[name]))
        for name in frame_types
    )
    return ProtocolModel(frame_types, aliases, tuple(builder.structs), header, seq_type, frames)
