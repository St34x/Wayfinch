# SPDX-License-Identifier: Apache-2.0
"""``config check`` never shows a credential-looking value: CLI output, gateway log, paths, unreadable YAML."""

from __future__ import annotations

import io
import logging

import pytest

from glasses import admin
from glasses import config as gcfg

from test_config_check_edges import EXTRA, SECRET, check_file, plat


@pytest.mark.parametrize(
    "data",
    [
        plat(extra={**EXTRA, "host": f"u:{SECRET}@127.0.0.1"}),
        plat(host=f"https://u:{SECRET}@127.0.0.1"),  # platform level
        {**plat(), "glasses": {"host": f"https://{SECRET}@x"}},  # root block
        {"platforms": {"glasses": {"enabled": True, "extra": {**EXTRA, "state_dir": f"/x/u:{SECRET}@h"}}}},
        {"gateway": {"glasses": {"enabled": True, "client_cert_header": f"X-{SECRET}@h", "extra": EXTRA}}},
        plat(allow_from=[f"https://u:{SECRET}@h"], dm_policy=f"u:{SECRET}@h"),
        plat(extra={**EXTRA, "public_url": f"wss://u:{SECRET}@glasses.example.com/ws"}),
        plat(
            extra={
                **EXTRA,
                "meeting": {
                    "endpoint": f"{SECRET}.example.com",
                    "provider": f"sk-{SECRET}",
                    "llm": {"provider": f"x://{SECRET}", "model": f"{SECRET}@m"},
                },
            }
        ),
        plat(extra={**EXTRA, "lanes": [f"u:{SECRET}@h", f"u:{SECRET}@h"]}),
    ],
    ids=[
        "extra.host",
        "platform-level host",
        "root host",
        "state_dir",
        "client_cert_header",
        "auth keys",
        "public_url userinfo",
        "meeting.*",
        "duplicate lanes",
    ],
)
def test_credential_values_not_in_cli_output_or_gateway_log(tmp_path, caplog, data):
    code, out = check_file(tmp_path, data)
    assert code in (admin.EXIT_OK, admin.EXIT_ERRORS) and SECRET not in out, out
    caplog.set_level(logging.DEBUG, logger="glasses")
    view = admin.platform_view(data)
    assert view is not None
    assert gcfg.validate_config({"extra": view.extra}) is (code == admin.EXIT_OK)
    assert SECRET not in caplog.text


def test_cli_paths_with_credentials_not_shown(tmp_path):
    out = io.StringIO()
    assert admin.main(["config", "check", "--config", f"/nonexistent/u:{SECRET}@h/config.yaml"], {}, out) == 2
    assert SECRET not in out.getvalue() and "path not shown" in out.getvalue()
    out = io.StringIO()
    args = ["config", "check", "--live", "--hermes-home", f"/n/u:{SECRET}@h", "--config", f"/n2/u:{SECRET}@h/c.yaml"]
    assert admin.main(args, {}, out) == 2 and SECRET not in out.getvalue()
    code, text = check_file(
        tmp_path, None, f"gateway:\n  platforms:\n    glasses:\n      extra: {{u: https://u:{SECRET}@h\n"
    )
    assert code == 2 and SECRET not in text and "not valid YAML at line" in text
