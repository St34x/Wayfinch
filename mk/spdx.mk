# SPDX-License-Identifier: Apache-2.0
# Licence gate (T1.47; Apache-2.0, see LICENSE): every tracked source file carries the SPDX header.
# Standalone: make -f mk/spdx.mk check-spdx

.PHONY: check-spdx
TEST_TARGETS += check-spdx

#: check-spdx — every tracked source file carries the SPDX licence header
check-spdx:
	sh tools/tests/test_check_spdx.sh
	sh tools/check-spdx.sh
