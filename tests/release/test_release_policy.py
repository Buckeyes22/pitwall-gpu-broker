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
    assert "body_path: docs/releases/${{ github.ref_name }}.md" in github_release


def test_release_candidate_requires_versioned_public_notes() -> None:
    validator = _load("validate_candidate", ROOT / "scripts/release/validate_candidate.py")
    assert validator.validate("v0.1.0a1", allow_dirty=True) == []
    notes = ROOT / "docs/releases/v0.1.0a1.md"
    assert notes.is_file()
    assert "GitHub-first" in notes.read_text(encoding="utf-8")


def test_python_registries_are_not_in_the_github_first_workflow() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "PITWALL_PYPI_RELEASE_ENABLED" not in workflow
    assert "publish-testpypi" not in workflow
    assert "publish-pypi" not in workflow
    assert "gh-action-pypi-publish" not in workflow


def test_release_automation_never_requests_provider_credentials() -> None:
    readiness = (ROOT / ".github/workflows/release-readiness.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    for workflow in (readiness, release):
        assert "RUNPOD_API_KEY" not in workflow
        assert "PITWALL_LIVE_" not in workflow
        assert "--run-live" not in workflow
        assert "live-provider-acceptance" not in workflow
    assert "secrets: inherit" not in release


def test_pull_requests_use_normal_ci_not_the_full_release_suite() -> None:
    readiness = (ROOT / ".github/workflows/release-readiness.yml").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "pull_request:" not in readiness
    assert "  required:" in ci
    assert "    name: CI" in ci


def test_live_endpoint_ids_are_external_inputs() -> None:
    lb_test = (ROOT / "tests/api/test_e2e_sync_inference.py").read_text(encoding="utf-8")
    queue_test = (ROOT / "tests/api/test_e2e_async_job_webhook.py").read_text(encoding="utf-8")

    assert "PITWALL_LIVE_LB_ENDPOINT_ID" in lb_test
    assert "PITWALL_LIVE_QUEUE_ENDPOINT_ID" in queue_test
    assert "eptest00000000" not in lb_test
    assert "rdhwjnr3j6b98y" not in queue_test
