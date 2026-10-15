# SPDX-License-Identifier: Apache-2.0
"""The private-key tree scan (``test_no_private_key_code.findings``) against more ways to load, generate or serialise a
private key: the names in the task's list are found under any import form, and so are the other key APIs of
``cryptography`` and an ``openssl`` key command."""

from __future__ import annotations

import pytest

from test_no_private_key_code import findings

ASYM = "from cryptography.hazmat.primitives.asymmetric"
SERIAL = "from cryptography.hazmat.primitives.serialization"
FOUND = {
    "aliased-import": f"{SERIAL} import load_pem_private_key as lp\n",
    "module-attribute": f"{ASYM} import rsa\nk = rsa.generate_private_key(65537, 2048)\n",
    "key-path-joined": "from pathlib import Path\nP = Path('ca') / ('ca' + '.key')\n",
    "key-path-f-string": "name = 'ca'\nP = f'{name}.key'\n",
    "nested-package": "def f(k):\n    return k.private_bytes(1, 2, 3)\n",
}


@pytest.mark.parametrize("case", sorted(FOUND))
def test_a_banned_name_under_any_import_form_is_found(tmp_path, case):
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "planted.py").write_text(FOUND[case], encoding="utf-8")
    assert findings(tmp_path)


OTHER_KEY_APIS = {
    "derive-private-key": f"{ASYM} import ec\nk = ec.derive_private_key(5, ec.SECP256R1())\n",
    "from-private-bytes": f"{ASYM}.ed25519 import Ed25519PrivateKey as K\nk = K.from_private_bytes(bytes(32))\n",
    "private-bytes-raw": "def f(k):\n    return k.private_bytes_raw()\n",
    "generate-classmethod": f"{ASYM}.ed25519 import Ed25519PrivateKey\nk = Ed25519PrivateKey.generate()\n",
    "load-pkcs12": f"{SERIAL} import pkcs12\nb = pkcs12.load_pkcs12(b'', None)\n",
    "openssl-genpkey": "import subprocess\nsubprocess.run(['openssl', 'genpkey', '-algorithm', 'ec'])\n",
}


@pytest.mark.parametrize("case", sorted(OTHER_KEY_APIS))
def test_another_private_key_api_is_found(tmp_path, case):
    (tmp_path / "planted.py").write_text(OTHER_KEY_APIS[case], encoding="utf-8")
    assert findings(tmp_path)
