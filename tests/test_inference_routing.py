"""Hermetic tests for POST /v1/inference routing."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.db.repository import CapabilityRepository, ProviderRepository

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _capability() -> Capability:
    return Capability(
        id="cap_embedding_bge_m3",
        name="embedding.bge-m3",
        version="1.0.0",
        class_="embedding",
        cost_mode="per_second",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(provider_id: str, *, priority: int, health_status: str = "healthy") -> Provider:
    return Provider(
        id=provider_id,
        capability_id="cap_embedding_bge_m3",
        name=provider_id,
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id=f"{provider_id}-endpoint",
        priority=priority,
        enabled=True,
        health_status=health_status,
        updated_at=_NOW,
    )


@pytest.fixture()
def api_client() -> tuple[object, AsyncMock, AsyncMock]:
    old = os.environ.copy()
    os.environ.update(
        {
            "RUNPOD_API_KEY": "test-key",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "REDIS_URL": "redis://localhost:6379/0",
        }
    )

    for mod in list(sys.modules):
        if mod.startswith("pitwall.api"):
            del sys.modules[mod]

    from pitwall.api.app import app
    from pitwall.api.routes.inference import _capability_repo, _provider_repo

    capability_repo = AsyncMock(spec=CapabilityRepository)
    provider_repo = AsyncMock(spec=ProviderRepository)
    app.state.pool = MagicMock()
    app.dependency_overrides[_capability_repo] = lambda: capability_repo
    app.dependency_overrides[_provider_repo] = lambda: provider_repo

    yield app, capability_repo, provider_repo

    app.dependency_overrides.clear()
    if hasattr(app.state, "pool"):
        delattr(app.state, "pool")
    os.environ.clear()
    os.environ.update(old)
    for mod in list(sys.modules):
        if mod.startswith("pitwall.api"):
            del sys.modules[mod]


@pytest.mark.anyio
async def test_inference_dry_run_routes_by_capability_name_to_priority_one_provider(
    api_client: tuple[object, AsyncMock, AsyncMock],
) -> None:
    app, capability_repo, provider_repo = api_client
    capability = _capability()
    capability_repo.get_by_name.return_value = capability
    capability_repo.get.return_value = None
    provider_repo.list.return_value = [
        _provider("prov_unhealthy", priority=1, health_status="unhealthy"),
        _provider("prov_priority_2", priority=2),
        _provider("prov_priority_1", priority=1),
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/inference",
            json={
                "capability": "embedding.bge-m3",
                "texts": ["hello"],
                "dry_run": True,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["workload_id"] == "dry_run_inference_cap_embe"
    assert body["result"]["capability_id"] == "cap_embedding_bge_m3"
    assert body["result"]["selected_provider_id"] == "prov_priority_1"
    assert body["result"]["eligible_provider_ids"] == [
        "prov_priority_1",
        "prov_priority_2",
    ]
    provider_repo.list.assert_awaited_once_with(
        capability_id="cap_embedding_bge_m3",
        enabled_only=True,
        limit=10,
    )


@pytest.mark.anyio
async def test_inference_unknown_explicit_provider_returns_404(
    api_client: tuple[object, AsyncMock, AsyncMock],
) -> None:
    app, capability_repo, provider_repo = api_client
    capability_repo.get_by_name.return_value = _capability()
    capability_repo.get.return_value = None
    provider_repo.get.return_value = None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/inference",
            json={
                "capability": "embedding.bge-m3",
                "provider_id": "prov_missing",
                "dry_run": True,
            },
        )

    assert resp.status_code == 404
    assert resp.json() == {"error": "provider_not_found", "id": "prov_missing"}
    provider_repo.get.assert_awaited_once_with("prov_missing")
    provider_repo.list.assert_not_awaited()
