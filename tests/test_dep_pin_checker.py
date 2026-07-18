"""Tests for the worker-owned dependency pin checker guard (L13)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.guards import dep_pin_checker

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_guard_passes_current_worker_owned_paths() -> None:
    repo_root = Path.cwd()
    assert dep_pin_checker.main(["--root", str(repo_root)]) == 0


def test_guard_fails_on_unpinned_torch_in_worker_dockerfile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir(parents=True)
    worker_dockerfile = docker_dir / "Dockerfile.worker-test"
    worker_dockerfile.write_text("RUN pip install torch torchaudio\n")

    result = dep_pin_checker.main(["--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "torch" in captured.err
    assert "unpinned" in captured.err


def test_guard_fails_on_unpinned_transformers_in_worker_dockerfile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir(parents=True)
    worker_dockerfile = docker_dir / "Dockerfile.worker-embed"
    worker_dockerfile.write_text("RUN pip install transformers huggingface_hub\n")

    result = dep_pin_checker.main(["--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "transformers" in captured.err
    assert "huggingface_hub" in captured.err


def test_guard_passes_on_pinned_deps_in_worker_dockerfile(tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir(parents=True)
    worker_dockerfile = docker_dir / "Dockerfile.operator-vllm"
    worker_dockerfile.write_text(
        "RUN pip install torch==2.5.0 torchaudio==2.5.0 transformers==4.46.0\n"
    )

    result = dep_pin_checker.main(["--root", str(tmp_path)])

    assert result == 0


def test_guard_passes_on_no_ml_packages(tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir(parents=True)
    worker_dockerfile = docker_dir / "Dockerfile.worker-basic"
    worker_dockerfile.write_text("RUN pip install fastapi pydantic\n")

    result = dep_pin_checker.main(["--root", str(tmp_path)])

    assert result == 0


def test_guard_ignores_non_worker_dockerfile(tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir(parents=True)
    api_dockerfile = docker_dir / "Dockerfile.api"
    api_dockerfile.write_text("RUN pip install torch\n")

    result = dep_pin_checker.main(["--root", str(tmp_path)])

    assert result == 0


def test_guard_fails_on_mixed_pinned_unpinned(tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir(parents=True)
    worker_dockerfile = docker_dir / "Dockerfile.worker-test"
    worker_dockerfile.write_text("RUN pip install torch==2.5.0 torchaudio\n")

    result = dep_pin_checker.main(["--root", str(tmp_path)])

    assert result == 1


def test_guard_fails_on_range_constraint(tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir(parents=True)
    worker_dockerfile = docker_dir / "Dockerfile.worker-test"
    worker_dockerfile.write_text("RUN pip install torch>=2.5\n")

    result = dep_pin_checker.main(["--root", str(tmp_path)])

    assert result == 1


def test_guard_handles_multiline_pip_install(tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir(parents=True)
    worker_dockerfile = docker_dir / "Dockerfile.worker-test"
    worker_dockerfile.write_text("RUN pip install \\\n    torch \\\n    torchaudio\n")

    result = dep_pin_checker.main(["--root", str(tmp_path)])

    assert result == 1


def test_guard_ignores_path_outside_worker_owned(tmp_path: Path) -> None:
    docs_file = tmp_path / "docs" / "README.md"
    docs_file.parent.mkdir(parents=True)
    docs_file.write_text("RUN pip install torch==2.5.0\n")

    result = dep_pin_checker.main(["--root", str(tmp_path), str(docs_file)])

    assert result == 0


class TestFixtureCoverage:
    """Verify the guard correctly detects unpinned ML packages in fixture files.

    These tests use check_file() directly to verify detection, since the
    fixtures are in tests/fixtures/guards/ (not worker-owned paths) and
    would be filtered out by iter_requested_files().
    """

    def test_check_file_detects_unpinned_in_dockerfile_fixture(self) -> None:
        fixture = _REPO_ROOT / "tests" / "fixtures" / "guards" / "Dockerfile.worker-dep-pin-fixture"
        assert fixture.exists(), f"Fixture not found: {fixture}"
        errors = dep_pin_checker.check_file(_REPO_ROOT, fixture)
        assert len(errors) >= 1, f"Expected at least 1 error, got: {errors}"
        assert any("torch" in e for e in errors)
        assert any("unpinned" in e for e in errors)
