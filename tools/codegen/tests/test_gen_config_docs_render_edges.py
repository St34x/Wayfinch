# SPDX-License-Identifier: Apache-2.0
"""Rendering edge cases of tools/codegen/gen_config_docs.py: list items that are mappings or lists without a
`type` word, and a mapping key whose values have a schema (`additionalProperties: {...}`), whose inner constraints the
table cannot show either.
"""

import copy
import json
from pathlib import Path
from typing import Any

import gen_config_docs as gen
import pytest
from protocol_model import CodegenError

ROOT = Path(__file__).resolve().parents[3]
SCHEMA: Any = json.loads((ROOT / gen.SCHEMA).read_text(encoding="utf-8"))


def _render_with(name: str, node: dict[str, Any]) -> str:
    schema = copy.deepcopy(SCHEMA)
    schema["properties"][name] = node
    return gen.render(schema)


@pytest.mark.parametrize(
    "items",
    [
        {"properties": {"lane": {"type": "string", "maxLength": 3}}},
        {"items": {"maxLength": 3}},
        {"additionalProperties": {"type": "string", "maxLength": 3}},
    ],
    ids=["properties-without-type", "items-without-type", "value-schema-without-type"],
)
def test_nested_items_without_a_type_word_fail_closed(items: dict[str, Any]) -> None:
    with pytest.raises(CodegenError, match=r"#/properties/planted/items: list items that are mappings or lists"):
        _render_with("planted", {"type": "array", "items": items})


@pytest.mark.xfail(
    strict=True,
    reason="known gap: a mapping key with a value schema (additionalProperties: {...}) and no properties "
    "renders as 'mapping' and silently drops the value constraints (maxLength 3 here); the docstring promises exit 2",
)
def test_mapping_with_a_value_schema_fails_closed() -> None:
    with pytest.raises(CodegenError):
        _render_with("planted", {"type": "object", "additionalProperties": {"type": "string", "maxLength": 3}})


def test_todays_schema_has_no_mapping_key_with_a_value_schema() -> None:
    """The gap above is latent: no key of the real schema is a mapping whose values carry a schema."""

    def walk(node: Any) -> list[str]:
        found = []
        for name, child in node.get("properties", {}).items():
            if isinstance(child.get("additionalProperties"), dict):
                found.append(name)
            found += walk(child)
        return found

    assert walk(SCHEMA) == []
