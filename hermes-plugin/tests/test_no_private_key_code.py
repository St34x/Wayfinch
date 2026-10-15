# SPDX-License-Identifier: Apache-2.0
"""SAT-SC-04, tree half: no file of the production plugin package ``hermes-plugin/glasses/`` (every text file, not
only ``*.py``; ``tests`` dirs and ``__pycache__`` excluded) loads, generates, serialises or signs with a private key,
holds PEM private-key text, runs an ``openssl`` key command or names a ``*.key`` file (the device CA key lives only
with the signer). In ``*.py`` the scan reads names in code (identifiers, attributes, imported names), calls and
string/bytes literals, so a comment may still mention the words; a literal equal to a banned name is a finding too
(``getattr(serialization, "load_pem_private_key")``). Other text files are matched line by line. A name built at run
time (``"load_pem_" + "private_key"``) is beyond a static scan. A planted copy in ``tmp_path`` proves each rule fires.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BANNED_NAMES = frozenset({
    "load_pem_private_key",
    "load_der_private_key",
    "load_ssh_private_key",
    "load_pkcs12",
    "load_key_and_certificates",
    "generate_private_key",
    "derive_private_key",
    "from_private_bytes",
    "private_numbers",
    "private_bytes",
    "private_bytes_raw",
})
PRIVATE_KEY_CLASSES = frozenset(f"{alg}PrivateKey" for alg in ("Ed25519", "Ed448", "X25519", "X448"))
PEM = "-" * 5  # PEM armour dashes; planted key text is built at run time so the secret scan stays quiet
BANNED_LITERALS = BANNED_NAMES | {"sign", "genpkey", "genrsa", "-genkey"}
_KEY_TEXT = re.compile(r"PRIVATE KEY-----|\bopenssl\s+(genpkey|genrsa|ecparam\b.*-genkey)")
_TEXT_RULES = re.compile(
    rf"\.sign\(|\b({'|'.join(sorted(BANNED_NAMES))})\b|({'|'.join(sorted(PRIVATE_KEY_CLASSES))})\.generate\("
    rf"|{_KEY_TEXT.pattern}"
)


def _literal_finding(value: str | bytes) -> str | None:
    text = value.decode("latin-1") if isinstance(value, bytes) else value
    if text.strip() in BANNED_LITERALS:
        return f"string literal {text.strip()!r}"
    if _KEY_TEXT.search(text):
        return "private-key text or openssl key command in a string literal"
    if text.casefold().endswith(".key"):
        return "string literal ending in .key"
    return None


def _call_finding(node: ast.Call, key_classes: set[str]) -> str | None:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "sign":
        return ".sign( call"
    receiver = func.value.attr if isinstance(func.value, ast.Attribute) else getattr(func.value, "id", "")
    if func.attr == "generate" and receiver in key_classes:
        return f"{receiver}.generate( call"
    return None


def _names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.alias):
        return node.name.split(".")
    return []


def _key_classes(tree: ast.AST) -> set[str]:
    """The private-key class names of *tree*, with the local names they are imported as."""
    aliases = {
        a.asname
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        for a in n.names
        if a.asname and a.name in PRIVATE_KEY_CLASSES
    }
    return set(PRIVATE_KEY_CLASSES) | aliases


def _node_findings(node: ast.AST, key_classes: set[str]) -> list[str]:
    found: list[str | None] = [name for name in _names(node) if name in BANNED_NAMES]
    if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
        found.append(_literal_finding(node.value))
    if isinstance(node, ast.Call):
        found.append(_call_finding(node, key_classes))
    return [what for what in found if what]


def _py_findings(text: str, name: str) -> list[str]:
    tree = ast.parse(text, filename=name)
    key_classes = _key_classes(tree)
    found = []
    for node in ast.walk(tree):
        found += [f"{name}:{getattr(node, 'lineno', 0)}: {what}" for what in _node_findings(node, key_classes)]
    return found


def _text_findings(text: str, name: str) -> list[str]:
    return [
        f"{name}:{i}: {m.group(0)}" for i, line in enumerate(text.splitlines(), 1) if (m := _TEXT_RULES.search(line))
    ]


def _scanned(root: Path) -> list[Path]:
    skip = {"tests", "__pycache__"}
    return sorted(p for p in root.rglob("*") if p.is_file() and not skip & set(p.relative_to(root).parts))


def findings(root: Path) -> list[str]:
    """``<file>:<line>: <what>`` for every private-key finding in the text files under *root* (binary files skipped)."""
    found = []
    for path in _scanned(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        name = str(path.relative_to(root))
        found += _py_findings(text, name) if path.suffix == ".py" else _text_findings(text, name)
    return found


def test_no_plugin_file_handles_a_private_key(plugin_dir):
    scanned = {p.suffix for p in _scanned(plugin_dir)}
    assert {".py", ".json", ".yaml", ".md"} <= scanned, f"the scan missed plugin files: {sorted(scanned)}"
    assert findings(plugin_dir) == []


ASYM = "from cryptography.hazmat.primitives.asymmetric"
LISTED_NAMES = [  # spelled out again, so dropping a name from BANNED_NAMES fails a case here
    "load_pem_private_key",
    "load_der_private_key",
    "load_ssh_private_key",
    "load_pkcs12",
    "load_key_and_certificates",
    "generate_private_key",
    "derive_private_key",
    "from_private_bytes",
    "private_numbers",
    "private_bytes",
    "private_bytes_raw",
]
PLANTED = {
    **{name: f"from cryptography.hazmat.primitives.serialization import {name}\n" for name in LISTED_NAMES},
    "attribute": "from cryptography.hazmat.primitives import serialization as s\ns.load_pem_private_key(b'', None)\n",
    "call": "def f(key):\n    return key.private_bytes(1, 2, 3)\n",
    "key-file": "PATH = 'ca/ca.key'\n",
    "key-file-upper": "PATH = b'CA.KEY'\n",
    "sign-call": "def f(builder, k, h):\n    return builder.sign(k, h)\n",
    **{f"generate-{c}": f"{ASYM} import {c}\nk = {c}.generate()\n" for c in sorted(PRIVATE_KEY_CLASSES)},
    "generate-aliased": f"{ASYM}.ed25519 import Ed25519PrivateKey as K\nk = K.generate()\n",
    "generate-module-attribute": f"{ASYM} import ed448\nk = ed448.Ed448PrivateKey.generate()\n",
    "pem-literal": f"X = '{PEM}BEGIN PRIVATE KEY{PEM}\\nMIIB\\n'\n",
    "pem-ec-bytes": f"X = b'{PEM}BEGIN EC PRIVATE KEY{PEM}'\n",
    "getattr-name": "import cryptography.hazmat.primitives.serialization as s\ngetattr(s, 'load_pem_private_key')\n",
    "getattr-sign": "def f(k, d):\n    return getattr(k, 'sign')(d)\n",
    "openssl-list": "import subprocess\nsubprocess.run(['openssl', 'genrsa', '2048'])\n",
    "openssl-string": "import os\nos.system('openssl ecparam -name prime256v1 -genkey -out x')\n",
    "openssl-genkey-arg": "ARGS = ['openssl', 'ecparam', '-genkey']\n",
}
PLANTED_TEXT = {
    "plugin.yaml": "run: openssl genpkey -algorithm ed25519\n",
    "config.schema.json": f'{{"x": "{PEM}BEGIN RSA PRIVATE KEY{PEM}"}}\n',
    "README.md": "call `key.sign(data)` here\n",
    "notes.txt": "k = Ed25519PrivateKey.generate()\n",
    "hook.sh": "python -c 'serialization.load_der_private_key(b)'\n",
}


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_a_planted_private_key_use_in_python_is_found(tmp_path, case):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "planted.py").write_text(PLANTED[case], encoding="utf-8")
    assert len(findings(tmp_path)) >= 1


@pytest.mark.parametrize("name", sorted(PLANTED_TEXT))
def test_a_planted_private_key_use_in_another_text_file_is_found(tmp_path, name):
    (tmp_path / name).write_text(PLANTED_TEXT[name], encoding="utf-8")
    assert len(findings(tmp_path)) == 1


def test_files_under_tests_and_binary_files_are_not_scanned(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixture.py").write_text(f"X = '{PEM}BEGIN PRIVATE KEY{PEM}'\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe private_bytes")
    assert findings(tmp_path) == []


def test_words_in_a_comment_or_docstring_are_not_findings(tmp_path):
    (tmp_path / "doc.py").write_text('"""no load_pem_private_key here"""\n# private_bytes .sign(\nX = "a.key.txt"\n')
    assert findings(tmp_path) == []
