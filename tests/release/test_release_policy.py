"""Release archive and workflow policy regression tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.release


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_workflows_satisfy_supply_chain_policy() -> None:
    module = _load("check_workflows", ROOT / "tools/ci/check_workflows.py")
    assert module.main() == 0


def test_dependency_compatibility_installs_the_selected_resolution_frozen() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    compatibility_job = workflow.split("  dependency-compatibility:", 1)[1].split(
        "\n  integration:", 1
    )[0]
    assert 'uv lock --upgrade --resolution "${{ matrix.resolution }}"' in compatibility_job
    assert "uv sync --frozen --extra dev" in compatibility_job
    assert 'uv run --frozen pytest -m "not integration and not slow"' in compatibility_job


def test_artifact_path_policy_rejects_private_and_traversal_paths() -> None:
    module = _load("inspect_artifacts", ROOT / "scripts/release/inspect_artifacts.py")
    assert module._safe("pitwall/module.py")
    assert not module._safe("../secret")
    assert not module._safe("project/.remember/events.jsonl")
    assert not module._safe("project/PRIVATE_EVIDENCE_PITWALL.md")


def test_github_first_release_requires_ghcr_but_not_deferred_pypi() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    github_release = workflow.split("  github-release:", 1)[1]
    assert "needs: [package, package-provenance, publish-image]" in github_release
    assert "publish-pypi" not in github_release
    assert "name: python-distributions" in github_release
    assert "name: package-evidence" in github_release


def test_only_deferred_python_registry_requires_a_separate_enable_gate() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert workflow.count("vars.PITWALL_PYPI_RELEASE_ENABLED == 'true'") == 2
    assert "PITWALL_GHCR_RELEASE_ENABLED" not in workflow
