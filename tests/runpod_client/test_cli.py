"""Tests for ``pitwall-gpu-broker register-template`` CLI command.

Covers:
- cache-hit output
- create output
- missing env handling
- registry auth selection
"""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall.runpod_client import templates
from tests.fakes.runpod import RunPodTemplateFake

pytestmark = pytest.mark.anyio


class TestRegisterTemplateCLI:
    """CLI-level tests for ``pitwall-gpu-broker register-template``."""

    def _cli(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the pitwall CLI with the given argv and return the result."""
        full_env = dict(os.environ)
        if env is not None:
            full_env.update(env)
        full_env["PYTHONPATH"] = "src"
        return subprocess.run(
            [sys.executable, "-m", "pitwall", *argv],
            capture_output=True,
            text=True,
            env=full_env,
        )

    def test_missing_runpod_api_key_exits_with_error(self) -> None:
        """When RUNPOD_API_KEY is not set, CLI prints an error and returns 1."""
        result = self._cli(
            ["register-template", "--image", "ghcr.io/org/worker:v1"],
            env={
                "RUNPOD_API_KEY": "",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 1
        assert "RUNPOD_API_KEY environment variable is not set" in result.stderr

    def test_dry_run_ghcr_image_shows_ghcr_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--dry-run`` with a GHCR image shows the GHCR registry auth."""
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "legacy-auth")
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_GHCR", "ghcr-auth")
        result = self._cli(
            ["register-template", "--image", "ghcr.io/org/worker:v1", "--dry-run"],
            env={
                "RUNPOD_API_KEY": "test-key",
                "RUNPOD_REGISTRY_AUTH_ID": "legacy-auth",
                "RUNPOD_REGISTRY_AUTH_ID_GHCR": "ghcr-auth",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 0
        assert "ghcr-auth" in result.stdout

    def test_dry_run_gitlab_registry_image_shows_gitlab_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--dry-run`` with a GitLab Registry image shows the GitLab registry auth."""
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "legacy-auth")
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_GITLAB", "glcr-auth")
        result = self._cli(
            [
                "register-template",
                "--image",
                "gitlab-registry.example.com/org/worker:v1",
                "--dry-run",
            ],
            env={
                "RUNPOD_API_KEY": "test-key",
                "RUNPOD_REGISTRY_AUTH_ID": "legacy-auth",
                "RUNPOD_REGISTRY_AUTH_ID_GITLAB": "glcr-auth",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 0
        assert "glcr-auth" in result.stdout

    def test_dry_run_docker_hub_image_shows_docker_hub_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--dry-run`` with a Docker Hub image shows the Docker Hub registry auth."""
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "legacy-auth")
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB", "dh-auth")
        result = self._cli(
            [
                "register-template",
                "--image",
                "docker.io/library/python:3.12",
                "--dry-run",
            ],
            env={
                "RUNPOD_API_KEY": "test-key",
                "RUNPOD_REGISTRY_AUTH_ID": "legacy-auth",
                "RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB": "dh-auth",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 0
        assert "dh-auth" in result.stdout

    def test_dry_run_unknown_registry_falls_back_to_legacy_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--dry-run`` with an unknown registry falls back to the legacy env var."""
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "legacy-auth")
        result = self._cli(
            [
                "register-template",
                "--image",
                "unknown.registry.com/org/worker:v1",
                "--dry-run",
            ],
            env={
                "RUNPOD_API_KEY": "test-key",
                "RUNPOD_REGISTRY_AUTH_ID": "legacy-auth",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 0
        assert "legacy-auth" in result.stdout

    def test_dry_run_outputs_image_and_template_details(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--dry-run`` outputs parsed image, template name, and container disk."""
        result = self._cli(
            [
                "register-template",
                "--image",
                "ghcr.io/org/worker:v1.2.3",
                "--template-name",
                "my-app",
                "--container-disk-gb",
                "80",
                "--dry-run",
            ],
            env={
                "RUNPOD_API_KEY": "test-key",
                "RUNPOD_REGISTRY_AUTH_ID": "legacy-auth",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 0
        assert "[dry-run] image: ghcr.io/org/worker:v1.2.3" in result.stdout
        assert "[dry-run] container_disk_gb: 80" in result.stdout
        assert "[dry-run] template_name: my-app" in result.stdout

    def test_dry_run_missing_required_arg(self) -> None:
        """``--dry-run`` with missing --image shows usage error."""
        result = self._cli(
            ["register-template", "--dry-run"],
            env={
                "RUNPOD_API_KEY": "test-key",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode != 0
        assert "error" in result.stderr.lower() or "required" in result.stderr.lower()


class TestRegisterTemplateAsync:
    """Async path tests for ``pitwall-gpu-broker register-template`` using mocked dependencies."""

    async def test_cache_hit_outputs_template_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runpod_template_fake: RunPodTemplateFake,
    ) -> None:
        """When template is cached, CLI outputs 'Template registered: <id>'."""
        image_ref = "ghcr.io/org/worker:abc123"
        cached_id = "template-cached-123"
        runpod_template_fake.set_cached(
            "pitwall-cloud-worker",
            templates.image_sha(image_ref),
            cached_id,
        )

        monkeypatch.setattr(templates, "_sdk", lambda: runpod_template_fake.sdk)
        monkeypatch.setattr(templates, "_lookup_cached", runpod_template_fake.lookup_cached)
        monkeypatch.setattr(templates, "_insert_cache", runpod_template_fake.insert_cache)

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_conn.fetchrow.return_value = {"template_id": cached_id}

        with patch("pitwall.db.get_pool", AsyncMock(return_value=mock_pool)):
            from argparse import Namespace

            from pitwall.cli import _register_template_async

            args = Namespace(
                image=image_ref,
                template_name="pitwall-cloud-worker",
                container_disk_gb=50,
                dry_run=False,
            )
            return_code = await _register_template_async(args)

        assert return_code == 0

    async def test_create_template_outputs_template_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runpod_template_fake: RunPodTemplateFake,
    ) -> None:
        """When no cached template exists, CLI creates one and outputs its ID."""
        image_ref = "ghcr.io/org/worker:new-image"

        monkeypatch.setattr(templates, "_sdk", lambda: runpod_template_fake.sdk)
        monkeypatch.setattr(templates, "_lookup_cached", runpod_template_fake.lookup_cached)
        monkeypatch.setattr(templates, "_insert_cache", runpod_template_fake.insert_cache)

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_conn.fetchrow.return_value = None

        with patch("pitwall.db.get_pool", AsyncMock(return_value=mock_pool)):
            from argparse import Namespace

            from pitwall.cli import _register_template_async

            args = Namespace(
                image=image_ref,
                template_name="pitwall-cloud-worker",
                container_disk_gb=50,
                dry_run=False,
            )
            return_code = await _register_template_async(args)

        assert return_code == 0
        assert runpod_template_fake.sdk.create_template_kwargs is not None

    async def test_missing_api_key_returns_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When RUNPOD_API_KEY is not set, _register_template_async returns 1."""
        monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

        from argparse import Namespace

        from pitwall.cli import _register_template_async

        args = Namespace(
            image="ghcr.io/org/worker:v1",
            template_name="pitwall-cloud-worker",
            container_disk_gb=50,
            dry_run=False,
        )
        return_code = await _register_template_async(args)

        assert return_code == 1
