from __future__ import annotations

import subprocess
from pathlib import Path

from tools.ci import check_markdown_links
from tools.ci.check_markdown_links import anchors, check_internal, link_targets, markdown_files


def test_link_targets_support_inline_images_and_references() -> None:
    text = "[doc](guide.md#start) ![image](asset.png)\n[policy]: POLICY.md\n"
    assert link_targets(text) == ["guide.md#start", "asset.png", "POLICY.md"]


def test_anchors_follow_github_style_duplicate_suffixes(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text('# Hello, World!\n## Hello World\n<a id="manual"></a>\n', encoding="utf-8")
    assert anchors(page) == {"hello-world", "hello-world-1", "manual"}


def test_internal_check_reports_missing_file_and_anchor(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    target = tmp_path / "guide.md"
    source.write_text("[ok](guide.md#start) [bad](guide.md#missing) [gone](gone.md)\n")
    target.write_text("# Start\n")

    failures = check_internal(tmp_path, [source, target])

    assert len(failures) == 2
    assert any("missing anchor #missing" in failure for failure in failures)
    assert any("missing target 'gone.md'" in failure for failure in failures)


def test_markdown_inventory_uses_tracked_and_nonignored_files(tmp_path: Path, monkeypatch) -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=b"README.md\0docs/guide.md\0src/module.py\0",
        stderr=b"",
    )
    monkeypatch.setattr(
        check_markdown_links.subprocess,
        "run",
        lambda *_args, **_kwargs: completed,
    )

    assert markdown_files(tmp_path) == [tmp_path / "README.md", tmp_path / "docs/guide.md"]
