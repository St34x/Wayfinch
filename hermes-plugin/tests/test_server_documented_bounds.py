# SPDX-License-Identifier: Apache-2.0
"""WS server bounds shared between the code, the envelope schema, the config schema and the two READMEs: each number
that two places state is compared here, so one cannot move without the other."""

from __future__ import annotations

import json
import re
from pathlib import Path

from aiohttp import WSCloseCode

from glasses import connections, envelope_scan, server, server_auth
from glasses.config_core import MAX_LANES

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_README = (ROOT / "protocol" / "README.md").read_text(encoding="utf-8")
PLUGIN_README = (ROOT / "hermes-plugin" / "glasses" / "README.md").read_text(encoding="utf-8")
ENVELOPE = json.loads((ROOT / "protocol" / "envelope.schema.json").read_text(encoding="utf-8"))
LIMITS = ENVELOPE["$defs"]["body.welcome"]["properties"]["limits"]["properties"]
KIB = 1024


def test_the_message_cap_is_the_codecs_frame_cap_and_fits_the_schemas_max_frame():
    assert server.MAX_MSG_SIZE == envelope_scan.FRAME_CAP == 65_536
    assert LIMITS["maxFrame"]["minimum"] <= server.MAX_MSG_SIZE <= LIMITS["maxFrame"]["maximum"]
    assert "up to 65 536 bytes (the `maxFrame` of `welcome`)" in PROTOCOL_README


def test_the_clip_cap_ceiling_is_the_schemas_max_audio_sec_and_the_lanes_ceiling_its_max_items():
    assert server.MAX_AUDIO_SEC == LIMITS["maxAudioSec"]["maximum"] == 30
    lanes = ENVELOPE["$defs"]["body.welcome"]["properties"]["lanes"]
    assert MAX_LANES == lanes["maxItems"] == 32 and "`lanes[]` (≤32)" in PROTOCOL_README


def test_the_byte_budget_can_pass_a_legal_frame_and_a_real_time_voice_clip():
    assert connections.INBOUND_BURST_BYTES >= server.MAX_MSG_SIZE  # else the largest legal frame is never accepted
    base64_audio_per_second = 16_000 * 2 * 4 // 3  # 16 kHz s16le PCM as base64 in audio.chunk frames
    assert 2 * base64_audio_per_second <= connections.INBOUND_BYTES_PER_S


def test_the_budget_numbers_of_the_code_are_the_numbers_of_both_readmes():
    assert connections.INBOUND_BYTES_PER_S == connections.INBOUND_BURST_BYTES == 128 * KIB
    assert connections.INBOUND_MIN_CHARGE == 256
    assert "128 KiB per second, burst 128 KiB, each frame charged at least 256 bytes" in PROTOCOL_README
    assert "128 KiB per second, burst 128 KiB, every frame charged at least 256 bytes" in PROTOCOL_README
    assert "128 KiB/s inbound byte budget per connection, over it `1008`" in PLUGIN_README


def test_the_close_codes_of_the_code_are_the_codes_of_the_protocol_table():
    rows = dict(re.findall(r"^\| `(\d{4})` \| `(\w+)` \|", PROTOCOL_README, re.MULTILINE))
    assert rows[str(server.CLOSE_REVOKED)] == "revoked"
    assert rows[str(connections.CLOSE_REPLACED)] == "replaced"
    assert rows[str(connections.CLOSE_GOING_AWAY)] == "going_away" == rows[str(WSCloseCode.GOING_AWAY)]
    assert rows[str(WSCloseCode.POLICY_VIOLATION)] == "policy_violation"
    assert rows[str(WSCloseCode.MESSAGE_TOO_BIG)] == "message_too_big"
    assert rows[str(WSCloseCode.TRY_AGAIN_LATER)] == "try_again_later"  # a hello whose revocation check fails
    assert rows[str(WSCloseCode.INTERNAL_ERROR)] == "internal_error"  # a failing on_frame or on_hello
    assert "a second change of the file during that read: the frame is dropped, the upgrade answered `503`" in (
        PROTOCOL_README
    )
    assert connections.ABORT_AFTER_S == 0.5
    assert "transport aborted if the close handshake takes >500 ms" in PROTOCOL_README


def test_the_upgrade_bounds_of_the_code_are_the_numbers_of_the_readmes():
    assert connections.UPGRADES_PER_CERT_MAX == 8
    assert (
        "already has 8 upgrades in flight" in PROTOCOL_README and "per verified client certificate" in PROTOCOL_README
    )
    assert "8 upgrades in flight per verified client certificate" in PLUGIN_README
    assert server_auth.HEADER_CAP == 512 and "over 512 bytes" in PROTOCOL_README
    assert server_auth.GATE_TIMEOUT == 5.0 and "does not answer within 5 s" in PROTOCOL_README
    assert (server_auth.HOP_WORKERS, server_auth.HOP_PENDING_MAX) == (4, 32)
    assert "4-thread pool, at most 32 requests deep" in PLUGIN_README
