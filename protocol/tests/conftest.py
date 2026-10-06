# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures of the protocol schema tests (topic split, T1.110). Constants and frame helpers: test_schema.py."""

import json

import pytest
from jsonschema import Draft202012Validator

from test_schema import SCHEMA_PATH


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator(schema):
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def v(validator):
    """Short name used by the test_schema_adversarial*.py files."""
    return validator
