"""Tests for the X-Pitwall-Secret admin guard middleware."""

from __future__ import annotations

import importlib
import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _env_for_app(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "RUNPOD_API_KEY": "test-key",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_app_module():
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]
    yield
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]


def _import_app(env: dict[str, str]):
    old = os.environ.copy()
    os.environ.update(env)
    for k in list(os.environ):
        if k not in env and k in (
            "RUNPOD_API_KEY",
            "DATABASE_URL",
            "REDIS_URL",
            "PITWALL_ADMIN_SECRET",
            "PITWALL_API_TOKEN",
            "PITWALL_INBOUND_RATE_LIMIT",
        ):
            del os.environ[k]
    try:
        mod = importlib.import_module("pitwall.api.app")
        return mod
    finally:
        os.environ.clear()
        os.environ.update(old)


@pytest.mark.anyio
class TestAdminSecretGuard:
    async def test_no_secret_env_rejects_admin_without_header(self):
        import httpx

        mod = _import_app(_env_for_app())
        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/admin/ping")
            assert resp.status_code == 401
            assert (
                resp.json()["detail"]
                == "admin routes disabled: PITWALL_ADMIN_SECRET is not configured"
            )

    async def test_secret_env_rejects_admin_without_header(self):
        import httpx

        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/admin/ping")
            assert resp.status_code == 401
            assert resp.json()["detail"] == "invalid or missing X-Pitwall-Secret"

    async def test_secret_env_rejects_wrong_secret(self):
        import httpx

        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/admin/ping", headers={"X-Pitwall-Secret": "wrong"})
            assert resp.status_code == 401

    async def test_secret_env_allows_correct_secret(self):
        import httpx

        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/admin/ping", headers={"X-Pitwall-Secret": "s3cret"})
            assert resp.status_code == 404

    async def test_health_endpoint_always_accessible(self):
        import httpx

        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

    async def test_health_endpoint_accessible_without_secret(self):
        import httpx

        mod = _import_app(_env_for_app())
        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200

    async def test_admin_root_path_blocked(self):
        import httpx

        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/admin")
            assert resp.status_code == 401

    async def test_non_admin_path_not_blocked(self):
        import httpx

        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/nonexistent-path")
            assert resp.status_code != 401

    async def test_budget_rejected_maps_to_http_402_body(self):
        import httpx

        from pitwall.cost import BudgetRejected, BudgetSnapshot

        mod = _import_app(_env_for_app())
        snapshot = BudgetSnapshot(
            monthly_budget_usd=Decimal("10.000000"),
            per_request_max_usd=Decimal("2.000000"),
            mtd_spend_usd=Decimal("9.500000"),
            estimate_usd=Decimal("0.750000"),
            budget_remaining_usd=Decimal("0.500000"),
        )

        @mod.app.get("/test-budget-rejected")
        async def test_budget_rejected() -> None:
            raise BudgetRejected("monthly_budget", snapshot)

        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/test-budget-rejected")

        assert resp.status_code == 402
        assert resp.json() == {
            "error": "budget_rejected",
            "reason": "monthly_budget",
            "snapshot": {
                "monthly_budget_usd": "10.000000",
                "per_request_max_usd": "2.000000",
                "mtd_spend_usd": "9.500000",
                "estimate_usd": "0.750000",
                "budget_remaining_usd": "0.500000",
            },
        }


@pytest.mark.anyio
class TestAdminAuthModeWithRealEndpoints:
    """Verify auth mode works correctly with actual admin endpoints."""

    async def test_provider_create_requires_auth_when_secret_set(self):
        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        mock_repo = AsyncMock()
        mock_pool = MagicMock()
        from pitwall.api.provider_routes import _capability_repo as capability_repo_dep
        from pitwall.api.provider_routes import _pool as pool_dep
        from pitwall.api.provider_routes import _repo as repo_dep

        mock_capability_repo = AsyncMock()
        mock_capability_repo.get.return_value = object()
        mod.app.dependency_overrides[repo_dep] = lambda: mock_repo
        mod.app.dependency_overrides[capability_repo_dep] = lambda: mock_capability_repo
        mod.app.dependency_overrides[pool_dep] = lambda: mock_pool

        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/admin/providers",
                json={
                    "capability_id": "cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
                    "name": "runpod-bge-m3-lb",
                    "provider_type": "serverless_lb",
                    "runpod_endpoint_id": "eptest00000000",
                },
            )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid or missing X-Pitwall-Secret"

    async def test_provider_create_rejects_wrong_secret(self):
        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        mock_repo = AsyncMock()
        mock_pool = MagicMock()
        from pitwall.api.provider_routes import _capability_repo as capability_repo_dep
        from pitwall.api.provider_routes import _pool as pool_dep
        from pitwall.api.provider_routes import _repo as repo_dep

        mock_capability_repo = AsyncMock()
        mock_capability_repo.get.return_value = object()
        mod.app.dependency_overrides[repo_dep] = lambda: mock_repo
        mod.app.dependency_overrides[capability_repo_dep] = lambda: mock_capability_repo
        mod.app.dependency_overrides[pool_dep] = lambda: mock_pool

        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/admin/providers",
                headers={"X-Pitwall-Secret": "wrong"},
                json={
                    "capability_id": "cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
                    "name": "runpod-bge-m3-lb",
                    "provider_type": "serverless_lb",
                },
            )
        assert resp.status_code == 401

    async def test_provider_create_succeeds_with_correct_secret(self):
        from datetime import UTC, datetime

        from pitwall.core.enums import CapabilitySource, ProviderType
        from pitwall.core.models import Provider

        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        mock_repo = AsyncMock()
        mock_pool = MagicMock()
        from pitwall.api.provider_routes import _capability_repo as capability_repo_dep
        from pitwall.api.provider_routes import _pool as pool_dep
        from pitwall.api.provider_routes import _repo as repo_dep

        mock_capability_repo = AsyncMock()
        mock_capability_repo.get.return_value = object()
        mod.app.dependency_overrides[repo_dep] = lambda: mock_repo
        mod.app.dependency_overrides[capability_repo_dep] = lambda: mock_capability_repo
        mod.app.dependency_overrides[pool_dep] = lambda: mock_pool

        mock_repo.get.return_value = None
        mock_repo.create.return_value = Provider(
            id="prov_01HQXR8K9N3JZQP7VW4MEX2YBA",
            capability_id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
            name="runpod-bge-m3-lb",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="eptest00000000",
            enabled=True,
            health_status="healthy",
            priority=0,
            source=CapabilitySource.API,
            updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
        )

        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock):
                resp = await client.post(
                    "/v1/admin/providers",
                    headers={"X-Pitwall-Secret": "s3cret"},
                    json={
                        "capability_id": "cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
                        "name": "runpod-bge-m3-lb",
                        "provider_type": "serverless_lb",
                        "runpod_endpoint_id": "eptest00000000",
                    },
                )
        assert resp.status_code == 201
        assert resp.json()["name"] == "runpod-bge-m3-lb"

    async def test_capability_create_requires_auth_when_secret_set(self):
        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        mock_repo = AsyncMock()
        mock_pool = MagicMock()
        from pitwall.api.capability_routes import _pool as pool_dep
        from pitwall.api.capability_routes import _repo as repo_dep

        mod.app.dependency_overrides[repo_dep] = lambda: mock_repo
        mod.app.dependency_overrides[pool_dep] = lambda: mock_pool

        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/admin/capabilities",
                json={
                    "name": "embedding.bge-m3",
                    "version": "1.0.0",
                    "class": "embedding",
                    "cost_mode": "per_second",
                },
            )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid or missing X-Pitwall-Secret"

    async def test_capability_create_rejects_wrong_secret(self):
        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        mock_repo = AsyncMock()
        mock_pool = MagicMock()
        from pitwall.api.capability_routes import _pool as pool_dep
        from pitwall.api.capability_routes import _repo as repo_dep

        mod.app.dependency_overrides[repo_dep] = lambda: mock_repo
        mod.app.dependency_overrides[pool_dep] = lambda: mock_pool

        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/admin/capabilities",
                headers={"X-Pitwall-Secret": "wrong"},
                json={
                    "name": "embedding.bge-m3",
                    "version": "1.0.0",
                    "class": "embedding",
                    "cost_mode": "per_second",
                },
            )
        assert resp.status_code == 401

    async def test_capability_create_succeeds_with_correct_secret(self):
        from datetime import UTC, datetime

        from pitwall.core.enums import (
            CapabilityClass,
            CapabilitySource,
            CostMode,
        )
        from pitwall.core.models import Capability

        mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET="s3cret"))
        mock_repo = AsyncMock()
        mock_pool = MagicMock()
        from pitwall.api.capability_routes import _pool as pool_dep
        from pitwall.api.capability_routes import _repo as repo_dep

        mod.app.dependency_overrides[repo_dep] = lambda: mock_repo
        mod.app.dependency_overrides[pool_dep] = lambda: mock_pool

        mock_repo.get_by_name.return_value = None
        mock_repo.create.return_value = Capability(
            id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
            name="embedding.bge-m3",
            version="1.0.0",
            class_=CapabilityClass.EMBEDDING,
            cost_mode=CostMode.PER_SECOND,
            enabled=True,
            hints_supported=[],
            source=CapabilitySource.API,
            created_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
        )

        transport = httpx.ASGITransport(app=mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "pitwall.api.capability_routes.insert_audit",
                new_callable=AsyncMock,
            ):
                resp = await client.post(
                    "/v1/admin/capabilities",
                    headers={"X-Pitwall-Secret": "s3cret"},
                    json={
                        "name": "embedding.bge-m3",
                        "version": "1.0.0",
                        "class": "embedding",
                        "cost_mode": "per_second",
                    },
                )
        assert resp.status_code == 201
        assert resp.json()["name"] == "embedding.bge-m3"
