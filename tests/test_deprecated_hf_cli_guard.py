"""Tests for the worker-owned deprecated HF CLI guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.guards import deprecated_hf_cli

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _deprecated_command() -> str:
    return " ".join(("huggingface-cli", "download"))


def test_guard_passes_current_worker_owned_paths() -> None:
    repo_root = Path.cwd()

    assert deprecated_hf_cli.main(["--root", str(repo_root)]) == 0


def test_guard_fails_on_deprecated_cli_in_worker_owned_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worker_entrypoint = tmp_path / "docker" / "operator-vllm-entrypoint.sh"
    worker_entrypoint.parent.mkdir(parents=True)
    worker_entrypoint.write_text(f"{_deprecated_command()} qwen/model\n")

    result = deprecated_hf_cli.main(["--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "docker/operator-vllm-entrypoint.sh:1" in captured.err
    assert _deprecated_command() in captured.err


def test_guard_ignores_deprecated_cli_outside_worker_owned_paths(
    tmp_path: Path,
) -> None:
    docs_file = tmp_path / "docs" / "operator" / "runbook.md"
    docs_file.parent.mkdir(parents=True)
    docs_file.write_text(f"`{_deprecated_command()}` is historical context only.\n")

    assert deprecated_hf_cli.main(["--root", str(tmp_path), str(docs_file)]) == 0


def test_guard_scans_worker_dockerfile_pattern(tmp_path: Path) -> None:
    worker_dockerfile = tmp_path / "docker" / "Dockerfile.worker-embed"
    worker_dockerfile.parent.mkdir(parents=True)
    worker_dockerfile.write_text(f"RUN {_deprecated_command()} bge/model\n")

    assert deprecated_hf_cli.main(["--root", str(tmp_path)]) == 1


class TestFixtureCoverage:
    """Verify the guard correctly detects deprecated HF CLI in fixture files.

    These tests use check_file() directly to verify detection, since the
    fixtures are in tests/fixtures/guards/ (not worker-owned paths) and
    would be filtered out by iter_requested_worker_files().
    """

    def test_check_file_detects_deprecated_in_shell_fixture(self, tmp_path: Path) -> None:
        fixture = tmp_path / "worker-fixture.sh"
        fixture.write_text(f"{_deprecated_command()} meta-llama/Llama-2-7b\n")
        errors = deprecated_hf_cli.check_file(_REPO_ROOT, fixture)
        assert len(errors) >= 1, f"Expected at least 1 error, got: {errors}"
        assert any(_deprecated_command() in e for e in errors)

    def test_check_file_detects_deprecated_in_dockerfile_fixture(self, tmp_path: Path) -> None:
        fixture = tmp_path / "Dockerfile.worker-fixture"
        fixture.write_text(f"RUN {_deprecated_command()} meta-llama/Llama-2-7b\n")
        errors = deprecated_hf_cli.check_file(_REPO_ROOT, fixture)
        assert len(errors) >= 1, f"Expected at least 1 error, got: {errors}"
        assert any(_deprecated_command() in e for e in errors)
