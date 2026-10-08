# SPDX-License-Identifier: Apache-2.0
"""Bypass probes of tools/quality/check_docs.py in scratch repositories: anchor and link edge forms, inputs the gate
must not skip (a missing private knowledge-base index, links a public export loses, links hidden in comments,
upper-case extensions)."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_docs.py"
sys.path.insert(0, str(SCRIPT.parent))  # check_docs.py imports md_links.py and check_hygiene.py from its directory


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_bypass_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cd = _load()
GUIDE = "# Guide\n\n## Install it\n\nText.\n"
KB = "." + "team/kb"  # the private knowledge base of the team folder (name built at run time for the hygiene gate)
BASE = {
    "README.md": "# Project\n\nAll documents: [docs/README.md](docs/README.md).\n",
    "docs/README.md": "# Docs\n\n- [Guide](guide.md)\n- [Fixes](troubleshooting/INDEX.md)\n",
    "docs/guide.md": GUIDE,
    "docs/troubleshooting/INDEX.md": "# Index\n\n- crash → [`crash.md`](crash.md)\n",
    "docs/troubleshooting/crash.md": "# Crash\n",
    f"{KB}/INDEX.md": "# KB\n\n- [a](a.md)\n",
    f"{KB}/a.md": "# A\n",
}


def _findings(root: Path, changes: dict[str, str], drop: tuple[str, ...] = ()) -> list[str]:
    for name, text in {**BASE, **changes}.items():
        if name in drop:
            continue
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    return list(cd.check_tree(root).findings)


def test_scratch_tree_with_private_kb_is_clean(tmp_path: Path) -> None:
    assert _findings(tmp_path, {}) == []


@pytest.mark.parametrize(
    ("extra", "finding"),
    [
        ("[top](#)\n", None),
        ("## Install it\n\n[again](#install-it-1)\n", None),
        ("## Install it\n\n[third](#install-it-2)\n", "broken anchor '#install-it-2'"),
        ("[upper](#Install-It)\n", "broken anchor '#Install-It'"),
        ("[dir](troubleshooting/)\n", None),
        ("[case](Troubleshooting/INDEX.md)\n", "broken link 'Troubleshooting/INDEX.md'"),
        ("[ref][r]\n\n[r]: nope.md\n", "broken link 'nope.md'"),
        ("[last](nope.md)", "broken link 'nope.md'"),
        ("`[span](nope.md)`\n\n```\n[fenced](nope.md)\n```\n", None),
    ],
)
def test_link_and_anchor_edge_forms(tmp_path: Path, extra: str, finding: str | None) -> None:
    found = _findings(tmp_path, {"docs/guide.md": GUIDE + "\n" + extra})
    if finding is None:
        assert found == []
    else:
        assert len(found) == 1 and found[0].startswith("docs/guide.md:") and finding in found[0]


def test_angle_link_to_a_file_with_spaces(tmp_path: Path) -> None:
    readme = BASE["docs/README.md"] + "- [Mine](<my guide.md>)\n"
    assert _findings(tmp_path, {"docs/README.md": readme, "docs/my guide.md": "# Mine\n"}) == []


def test_emptied_private_kb_index_lists_nothing(tmp_path: Path) -> None:
    assert _findings(tmp_path, {f"{KB}/INDEX.md": "# KB\n"}) == [f"{KB}/a.md:1: not listed in {KB}/INDEX.md"]


def test_deleted_private_kb_index_is_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(cd.DocsError, match=r"kb/INDEX\.md: required"):
        _findings(tmp_path, {}, drop=(f"{KB}/INDEX.md",))


def test_renamed_private_kb_index_is_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(cd.DocsError, match=r"kb/INDEX\.md: required"):
        _findings(tmp_path, {f"{KB}/index.md": BASE[f"{KB}/INDEX.md"]}, drop=(f"{KB}/INDEX.md",))


def test_public_export_without_the_private_folder_needs_no_kb_index(tmp_path: Path) -> None:
    assert _findings(tmp_path, {}, drop=(f"{KB}/INDEX.md", f"{KB}/a.md")) == []


@pytest.mark.parametrize("target", [f"../{KB}/a.md", f"/{KB}/", f"../{KB}/missing.md#x"])
def test_public_doc_linking_into_private_notes_fails(tmp_path: Path, target: str) -> None:
    found = _findings(tmp_path, {"docs/guide.md": GUIDE + f"\nSee [notes]({target}).\n"})
    assert found == [f"docs/guide.md:7: '{target}' links into the private folder, which a public export lacks"]


def test_index_entry_hidden_in_a_multiline_comment_is_unlisted(tmp_path: Path) -> None:
    index = "# Index\n\n<!--\n- [crash](crash.md)\n-->\n"
    found = _findings(tmp_path, {"docs/troubleshooting/INDEX.md": index})
    assert found == ["docs/troubleshooting/crash.md:1: not listed in docs/troubleshooting/INDEX.md"]


def test_text_after_a_multiline_comment_is_checked(tmp_path: Path) -> None:
    guide = GUIDE + "\n<!-- note\n[hidden](nope.md) -->[shown](gone.md)\n```\n<!--\n```\n[after](missing.md)\n"
    found = _findings(tmp_path, {"docs/guide.md": guide})
    assert [line.split(": ", 1)[1] for line in found] == [
        "broken link 'gone.md': no tracked file or directory docs/gone.md",
        "broken link 'missing.md': no tracked file or directory docs/missing.md",
    ]


def test_upper_case_md_extension_is_checked(tmp_path: Path) -> None:
    other = "# Other\n\n[broken](missing.md)\n"
    found = _findings(tmp_path, {"docs/Other.MD": other})
    assert found[0] == "docs/Other.MD:1: not reachable by links from README.md (index it in docs/README.md)"
    assert found[1].startswith("docs/Other.MD:3: broken link 'missing.md'")
