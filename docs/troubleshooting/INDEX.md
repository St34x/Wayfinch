<!-- SPDX-License-Identifier: Apache-2.0 -->
# Troubleshooting — index

Search this list (`grep -i '<error text>' docs/troubleshooting/INDEX.md`) before debugging. One line per entry:
symptom keywords → file. Format of every entry (one file per issue, file name = slug): **Symptom** (exact error text, grep-able) ·
**Context** · **Cause** · **Fix** (the only working way) · **Verified** (evidence) · **Related**.
When you solve a new problem that other deployers or contributors can hit, add an entry and a line here in the same
commit.

- adb no permissions, plugdev, uaccess, SSH user, udev → [`adb-no-permissions-plugdev-ssh.md`](adb-no-permissions-plugdev-ssh.md)
- API 37 emulator crash loop, surfaceflinger, hasReadColorBufferDma → [`android-emulator-api37-surfaceflinger-crash.md`](android-emulator-api37-surfaceflinger-crash.md)
- tcpdump empty, no DNS names, Wi-Fi not captured, private DNS DoT, timestamps off 3600 s → [`android-emulator-egress-capture-empty.md`](android-emulator-egress-capture-empty.md)
- 16 KB alignment, Align 0x1000, readelf LOAD, Play page size → [`android-native-lib-16kb-alignment.md`](android-native-lib-16kb-alignment.md)
- metadata_errors, PyAV 19, faster-whisper, every clip fails → [`faster-whisper-pyav-metadata-errors.md`](faster-whisper-pyav-metadata-errors.md)
- GLASSES_ALLOW_ALL_USERS is set, wildcard '*', GATEWAY_ALLOW_ALL_USERS, platform refused → [`glasses-platform-refused-allow-all.md`](glasses-platform-refused-allow-all.md)
- AF_UNIX path too long, shutdown_watchdog, loop-tick socket → [`hermes-af-unix-path-too-long.md`](hermes-af-unix-path-too-long.md)
- gateway exit 78, non-retryable startup conflict, plugin connect fails → [`hermes-gateway-exit-78-plugin-startup.md`](hermes-gateway-exit-78-plugin-startup.md)
- mcp__phone__ missing, tool_search, deferred MCP tools → [`hermes-mcp-tools-deferred-tool-search.md`](hermes-mcp-tools-deferred-tool-search.md)
- no tools on glasses, n_tools: 0, platform_toolsets → [`hermes-plugin-platform-has-no-tools.md`](hermes-plugin-platform-has-no-tools.md)
- Module "tools" has no attribute, mypy, attr-defined, Hermes tools package, namespace_packages → [`mypy-tools-namespace-clash.md`](mypy-tools-namespace-clash.md)
- no answer on a lane, profile_routes, missing profile, WARNING only → [`hermes-route-to-missing-profile-dropped.md`](hermes-route-to-missing-profile-dropped.md)
- voice translated to English, stt.language en, wrong language → [`hermes-stt-language-en-translates.md`](hermes-stt-language-en-translates.md)
- CRL has expired, 12:CRL has expired, all devices rejected, status 444 → [`nginx-crl-expired-rejects-all-devices.md`](nginx-crl-expired-rejects-all-devices.md)
- offline hash test, PIP_FIND_LINKS, tampered wheel downloaded anyway, no sdist fallback, file:// index → [`pip-find-links-no-hash-prefilter.md`](pip-find-links-no-hash-prefilter.md)
- Hashes are required in --require-hashes mode, DO NOT MATCH THE HASHES, make venv → [`pip-require-hashes-missing-hash.md`](pip-require-hashes-missing-hash.md)
- tampered hash still installs, Building wheel, sdist fallback, --require-hashes exit 0 → [`pip-tampered-wheel-hash-installs-sdist.md`](pip-tampered-wheel-hash-installs-sdist.md)
- ruff too-many-arguments without PLR0913, rule names instead of codes, preview → [`ruff-preview-shows-rule-names.md`](ruff-preview-shows-rule-names.md)
- gitleaks failed, tool error not a leak, FTL unable to load gitleaks config, exit codes → [`secret-scan-tool-error-not-a-leak.md`](secret-scan-tool-error-not-a-leak.md)
- Forbidden: bot was blocked by the user, long answer failed → [`telegram-bot-blocked-forbidden.md`](telegram-bot-blocked-forbidden.md)
- the Python module 'jsonschema' is not installed, glasses config invalid, config check exit 2 → [`glasses-jsonschema-not-installed.md`](glasses-jsonschema-not-installed.md)
- mypy Success but untyped code, disable_error_code, mypy: ignore-errors, module-level type: ignore, no_type_check, check-mypy-config → [`mypy-strict-silently-bypassed.md`](mypy-strict-silently-bypassed.md)
- Module does not exist, No matches for ignored import, subpackages of external packages are not valid, lint-imports, import-linter → [`import-linter-contract-errors.md`](import-linter-contract-errors.md)
- io.open passes, Contracts: 1 kept, 0 broken, denylist, import requests in core, datetime.now, allowlist → [`import-linter-denylist-passes-io.md`](import-linter-denylist-passes-io.md)
- multiple exception types must be parenthesized, except A, B, Python 3.11, py314, ruff format → [`python-311-except-tuple-ruff-format.md`](python-311-except-tuple-ruff-format.md)
- No module named 'ruamel', test Hermes venv, Python older than 3.14, pip installs almost nothing → [`test-hermes-venv-no-module-ruamel.md`](test-hermes-venv-no-module-ruamel.md)
- the Python module 'PyYAML' is not installed, config check --live exit 2, test_config_check_live fails → [`test-hermes-venv-pyyaml-missing.md`](test-hermes-venv-pyyaml-missing.md)
