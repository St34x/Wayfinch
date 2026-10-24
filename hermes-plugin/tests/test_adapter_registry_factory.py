# SPDX-License-Identifier: Apache-2.0
"""The registry of ``/ws`` comes from the factory only (T1.184b): a hand-built ``SignerFileRegistry`` can be
given the plugin's own uid as signer and only ``signer_file_registry(cfg)`` refuses that pair, and a plain ``Registry``
on the state dir would be a writable one. So in the production tree ``SignerFileRegistry`` is built in the factory
alone, ``Registry`` nowhere, and the adapter calls the factory once."""

from __future__ import annotations

import ast
from pathlib import Path

GLASSES = Path(__file__).resolve().parent.parent / "glasses"
CONSTRUCTORS = {"Registry", "SignerFileRegistry"}


def _calls(path: Path) -> list[tuple[str, str]]:
    """(enclosing function, called name) of every call of a registry constructor or of the factory in *path*."""
    found: list[tuple[str, str]] = []

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else scope
            if isinstance(child, ast.Call):
                name = child.func.id if isinstance(child.func, ast.Name) else getattr(child.func, "attr", "")
                if name in CONSTRUCTORS | {"signer_file_registry"}:
                    found.append((scope, name))
            walk(child, inner)

    walk(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def test_the_registry_constructors_are_called_in_the_factory_only() -> None:
    constructed = {
        (path.name, scope, name)
        for path in sorted(GLASSES.glob("*.py"))
        for scope, name in _calls(path)
        if name in CONSTRUCTORS
    }
    assert constructed == {("registry_signer_file.py", "signer_file_registry", "SignerFileRegistry")}


def _connect_calls(function: str = "connect") -> set[str]:
    tree = ast.parse((GLASSES / "adapter.py").read_text(encoding="utf-8"))
    connect = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == function)
    return {c.func.id for c in ast.walk(connect) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}


def test_the_adapter_wires_the_registry_through_the_factory_once_from_the_start_path() -> None:
    factory = [(s, n) for s, n in _calls(GLASSES / "adapter.py") if n == "signer_file_registry"]
    assert factory == [("_make", "signer_file_registry")]  # built with the outbox and the frame check over it (T1.248)
    assert "_make" in _connect_calls("_start_edge")  # the start path builds those parts once
    assert "_start_edge" in _connect_calls()  # connect() reaches the factory through the start path
