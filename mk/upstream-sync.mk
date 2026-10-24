# SPDX-License-Identifier: Apache-2.0
# Vendored-driver import check (T1.84): tools/upstream-sync/verify-import.sh proves the copied files equal the pinned
# upstream blobs apart from the documented header lines and package rewrite; its tests plant one defect per case.
# Offline (recorded hashes only), no JDK, no network. While the vendored files are unmodified it checks the work tree.
# The first task that changes a vendored file by design (G2Host seam, T1.85) must set UPSTREAM_IMPORT_REV to the import
# commit, so the check keeps reading that commit instead of the modified files.
# Standalone: make -f mk/upstream-sync.mk check-upstream-import
UPSTREAM_IMPORT_REV ?=

.PHONY: check-upstream-import
TEST_TARGETS += check-upstream-import

#: check-upstream-import — the vendored G2 driver equals the pinned upstream files apart from the documented changes (offline; its tests plant one defect per case)
check-upstream-import:
	sh tools/upstream-sync/tests/test_verify_import.sh
	sh tools/upstream-sync/verify-import.sh $(if $(UPSTREAM_IMPORT_REV),--rev $(UPSTREAM_IMPORT_REV))
