from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

from pitwall.api.schemas.leases import LeasePatch, lease_patch_conflicting_fields
from pitwall.db.repository import LeaseRepository


@pytest.fixture()
def lease_api_client():
    old = os.environ.copy()
    os.environ.update(
        {
            "RUNPOD_API_KEY": "test-key",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "REDIS_URL": "redis://localhost:6379/0",
        }
    )
    os.environ.pop("PITWALL_ADMIN_SECRET", None)

    for mod in list(sys.modules):
        if mod.startswith("pitwall.api"):
            del sys.modules[mod]

    from pitwall.api.app import app
    from pitwall.api.routes.leases import _lease_repo as lease_repo_dep

    mock_repo = AsyncMock(spec=LeaseRepository)
    app.dependency_overrides[lease_repo_dep] = lambda: mock_repo

    yield app, mock_repo

    app.dependency_overrides.clear()
    os.environ.clear()
    os.environ.update(old)
    for mod in list(sys.modules):
        if mod.startswith("pitwall.api"):
            del sys.modules[mod]


def test_lease_patch_change_set_reports_exact_raw_fields() -> None:
    fields = lease_patch_conflicting_fields(
        {
            "renewal_policy": "manual",
            "image_ref": "ghcr.io/acme/pitwall-worker:sha-1",
            "gpuTypeIds": ["NVIDIA L4"],
            "volume_id": None,
        }
    )

    assert fields == ["image_ref", "gpuTypeIds", "volume_id"]


def test_lease_patch_change_set_allows_single_axis_changes() -> None:
    patch = LeasePatch(
        image_ref="ghcr.io/acme/pitwall-worker:sha-1",
        template_name="pitwall-qwen3",
        renewal_policy="manual",
    )

    assert lease_patch_conflicting_fields(patch) == []


@pytest.mark.anyio
async def test_patch_lease_rejects_multi_axis_change_set(lease_api_client: tuple) -> None:
    app, mock_repo = lease_api_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/v1/leases/lease_01",
            json={
                "image_ref": "ghcr.io/acme/pitwall-worker:sha-1",
                "gpu_type_priority": ["NVIDIA L4"],
                "volume_id": "vol_model_cache",
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": "change_set_too_broad",
        "conflicting_fields": ["image_ref", "gpu_type_priority", "volume_id"],
    }
    mock_repo.get.assert_not_called()
