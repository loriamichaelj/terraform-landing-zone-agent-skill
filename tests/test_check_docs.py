"""Tests for tools/check_docs.py (links and anchors)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools/check_docs.py"
_spec = importlib.util.spec_from_file_location("check_docs", TOOL)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)


def findings(root: Path) -> list[str]:
    files = sorted(root.rglob("*.md"))
    return cd.check_links(root, files)


def test_repo_docs_are_consistent():
    assert cd.main([str(ROOT)]) == 0


def test_slug_matches_github():
    assert cd.slug("Step 2: Render the spec with `lzctl`; check it") == "step-2-render-the-spec-with-lzctl-check-it"
    assert cd.slug("Cost estimate") == "cost-estimate"
    assert cd.slug("GCP and GitHub environment reference") == "gcp-and-github-environment-reference"


def test_missing_link_target_is_found(tmp_path):
    (tmp_path / "a.md").write_text("[x](missing.md) and ![i](img/none.png)\n")
    assert [m.split(": ", 1)[1] for m in findings(tmp_path)] == ["link target not found: missing.md", "link target not found: img/none.png"]


def test_missing_anchor_is_found(tmp_path):
    (tmp_path / "a.md").write_text("# Title\n\n[ok](#title) [bad](#nope) [other](b.md#section) [bad2](b.md#absent)\n")
    (tmp_path / "b.md").write_text("## Section\n")
    assert [m.split(": ", 1)[1] for m in findings(tmp_path)] == ["no heading for anchor: #nope", "no heading for anchor: b.md#absent"]


def test_duplicate_headings_get_numbered_anchors(tmp_path):
    (tmp_path / "a.md").write_text("## Same\n\n## Same\n\n[one](#same) [two](#same-1) [three](#same-2)\n")
    assert [m.split(": ", 1)[1] for m in findings(tmp_path)] == ["no heading for anchor: #same-2"]


def test_external_links_code_and_fences_are_ignored(tmp_path):
    (tmp_path / "a.md").write_text(
        "[web](https://example.com/x) [mail](mailto:a@b.c) `[code](nope.md)`\n\n```\n[fenced](nope.md)\n# not-a-heading\n```\n"
    )
    assert findings(tmp_path) == []


def test_links_cannot_escape_the_repo(tmp_path):
    (tmp_path / "outside.md").write_text("# Hi\n")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.md").write_text("[x](../outside.md)\n")
    assert "link target not found" in findings(root)[0]


def test_cli_exit_codes(tmp_path):
    (tmp_path / "a.md").write_text("[x](missing.md)\n")
    run = lambda *a: subprocess.run([sys.executable, str(TOOL), *a], capture_output=True, text=True)  # noqa: E731
    assert run(str(ROOT)).returncode == 0
    assert run(str(tmp_path)).returncode == 1
    assert run(str(tmp_path / "nope")).returncode == 2


def test_vendored_upstream_markdown_is_not_checked(tmp_path):
    vendored = tmp_path / "templates/fast/upstream/dataset"
    vendored.mkdir(parents=True)
    (vendored / "README.md").write_text("[broken](../../../../nowhere.md)\n")
    (tmp_path / "a.md").write_text("[ok](a.md)\n")
    assert [p.name for p in cd.markdown_files(tmp_path)] == ["a.md"]
    assert cd.main([str(tmp_path)]) == 0
