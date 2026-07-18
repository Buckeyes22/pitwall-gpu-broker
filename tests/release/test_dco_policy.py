"""DCO policy parser regression tests."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.release


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools" / "ci" / "check_dco.py"
    spec = importlib.util.spec_from_file_location("check_dco", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_dco_parser_accepts_valid_trailer_and_rejects_missing() -> None:
    module = _module()
    commits = module.parse_git_log(
        "a" * 40
        + "\0fix: safe change\n\nSigned-off-by: Example Contributor <dev@example.com>\n\0"
        + "b" * 40
        + "\0fix: unsigned change\n\0"
    )
    assert module.unsigned_commits(commits) == ["b" * 40]


def test_dco_parser_rejects_malformed_identity() -> None:
    module = _module()
    commits = module.parse_git_log(
        "c" * 40 + "\0docs: update\n\nSigned-off-by: missing-address\n\0"
    )
    assert module.unsigned_commits(commits) == ["c" * 40]


def test_dco_reads_real_git_log_records(tmp_path: Path) -> None:
    module = _module()
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Test Contributor"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "dev@example.com"], check=True)
    subprocess.run(["git", "-C", repo, "config", "commit.gpgsign", "false"], check=True)

    tracked = repo / "example.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "example.txt"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--quiet", "-m", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    tracked.write_text("updated\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "example.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "commit",
            "--quiet",
            "-m",
            "update",
            "-m",
            "Signed-off-by: Test Contributor <dev@example.com>",
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "log",
            "--no-merges",
            "-z",
            "--format=%H%x00%B",
            f"{base}..{head}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    commits = module.parse_git_log(result.stdout)
    assert [commit.sha for commit in commits] == [head]
    assert module.unsigned_commits(commits) == []
