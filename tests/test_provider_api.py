"""API tests for the Provider CRUD surface.

Covers happy paths, duplicate name rejection, schema validation,
enable/disable/hibernate toggling, health endpoint, and config audit
row creation.  All tests are hermetic — no database or external service
required.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pitwall.core.enums import CapabilitySource, ProviderType
from pitwall.core.models import Provider
from pitwall.db.repository import ProviderRepository

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
ADMIN_SECRET = "test-admin-secret"
ADMIN_HEADERS = {"X-Pitwall-Secret": ADMIN_SECRET}


def _make_provider(
    id: str = "prov_01HQXR8K9N3JZQP7VW4MEX2YBA",
    capability_id: str = "cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
    name: str = "runpod-bge-m3-lb",
    provider_type: ProviderType = ProviderType.SERVERLESS_LB,
    enabled: bool = True,
    health_status: str = "healthy",
    priority: int = 0,
    region: str | None = None,
    cloud_type: str | None = None,
    config: dict[str, object] | None = None,
    runpod_endpoint_id: str | None = None,
    runpod_template_id: str | None = None,
    cold_start_p50_ms: int | None = None,
    cold_start_p95_ms: int | None = None,
    recent_error_rate: float = 0.0,
    source: CapabilitySource = CapabilitySource.API,
) -> Provider:
    return Provider(
        id=id,
        capability_id=capability_id,
        name=name,
        provider_type=provider_type,
        runpod_endpoint_id=runpod_endpoint_id,
        runpod_template_id=runpod_template_id,
        region=region,
        cloud_type=cloud_type,
        config=config or {},
        priority=priority,
        enabled=enabled,
        health_status=health_status,
        cold_start_p50_ms=cold_start_p50_ms,
        cold_start_p95_ms=cold_start_p95_ms,
        recent_error_rate=recent_error_rate,
        source=source,
        updated_at=_NOW,
    )


def _base_create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "capability_id": "cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
        "name": "runpod-bge-m3-lb",
        "provider_type": "serverless_lb",
        "runpod_endpoint_id": "eptest00000000",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def mock_repo() -> AsyncMock:
    return AsyncMock(spec=ProviderRepository)


@pytest.fixture()
def api_client(mock_repo: AsyncMock):
    old = os.environ.copy()
    os.environ.update(
        {
            "RUNPOD_API_KEY": "test-key",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "PITWALL_ADMIN_SECRET": ADMIN_SECRET,
        }
    )

    for mod in list(sys.modules):
        if mod.startswith("pitwall.api"):
            del sys.modules[mod]

    from pitwall.api.app import app
    from pitwall.api.provider_routes import _capability_repo as capability_repo_dep
    from pitwall.api.provider_routes import _pool as pool_dep
    from pitwall.api.provider_routes import _repo as repo_dep

    mock_capability_repo = AsyncMock()
    mock_capability_repo.get.return_value = object()
    mock_pool = MagicMock()
    app.dependency_overrides[repo_dep] = lambda: mock_repo
    app.dependency_overrides[capability_repo_dep] = lambda: mock_capability_repo
    app.dependency_overrides[pool_dep] = lambda: mock_pool

    yield app, mock_repo, mock_pool

    app.dependency_overrides.clear()
    os.environ.clear()
    os.environ.update(old)
    for mod in list(sys.modules):
        if mod.startswith("pitwall.api"):
            del sys.modules[mod]


VALID_CREATE = _base_create_payload()


@pytest.mark.anyio
class TestCreateProvider:
    async def test_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None
        mock_repo.create.return_value = _make_provider()

        with patch("pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post("/v1/admin/providers", json=VALID_CREATE)

        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "runpod-bge-m3-lb"
        assert body["provider_type"] == "serverless_lb"
        assert body["enabled"] is True
        assert "id" in body
        assert "updated_at" in body

    async def test_with_all_optional_fields(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None
        mock_repo.create.return_value = _make_provider(
            runpod_endpoint_id="ep_abc123",
            runpod_template_id="tpl_xyz789",
            region="US",
            cloud_type="SECURE",
            config={"gpu_type": "NVIDIA A100 80GB"},
            priority=5,
            cold_start_p50_ms=1200,
            cold_start_p95_ms=3500,
            recent_error_rate=0.02,
            source=CapabilitySource.YAML,
        )

        payload = _base_create_payload(
            runpod_endpoint_id="ep_abc123",
            runpod_template_id="tpl_xyz789",
            region="US",
            cloud_type="SECURE",
            config={"gpu_type": "NVIDIA A100 80GB"},
            priority=5,
            cold_start_p50_ms=1200,
            cold_start_p95_ms=3500,
            recent_error_rate=0.02,
            source="yaml",
        )

        with patch("pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post("/v1/admin/providers", json=payload)

        assert resp.status_code == 201
        body = resp.json()
        assert body["runpod_endpoint_id"] == "ep_abc123"
        assert body["region"] == "US"
        assert body["priority"] == 5
        assert body["source"] == "yaml"

    async def test_duplicate_name_returns_409(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = _make_provider()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/providers", json=VALID_CREATE)

        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "provider_conflict"
        assert body["name"] == "runpod-bge-m3-lb"

    async def test_schema_validation_missing_required_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/providers", json={})

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        missing = {e["loc"][-1] for e in detail}
        assert "capability_id" in missing
        assert "name" in missing

    async def test_schema_validation_empty_name_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post(
                "/v1/admin/providers",
                json=_base_create_payload(name=""),
            )

        assert resp.status_code == 422

    async def test_schema_validation_invalid_provider_type_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post(
                "/v1/admin/providers",
                json=_base_create_payload(provider_type="not_real"),
            )

        assert resp.status_code == 422

    async def test_create_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        mock_repo.get.return_value = None
        mock_repo.create.return_value = _make_provider()

        with patch(
            "pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post("/v1/admin/providers", json=VALID_CREATE)

        assert resp.status_code == 201
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["actor"] == "rest:admin"
        assert call_kwargs[1]["action"] == "create"
        assert call_kwargs[1]["entity_type"] == "provider"
        assert call_kwargs[1]["new_value"] is not None


@pytest.mark.anyio
class TestListProviders:
    async def test_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.list.return_value = [
            _make_provider(name="prov-a"),
            _make_provider(id="prov_02ANOTHERULID0000000000", name="prov-b"),
        ]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/providers")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    async def test_empty_list(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.list.return_value = []

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/providers")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []

    async def test_enabled_filter_passes_enabled_only_true(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.list.return_value = [_make_provider()]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/providers?enabled=true")

        assert resp.status_code == 200
        mock_repo.list.assert_called_once_with(
            capability_id=None,
            enabled_only=True,
            provider_type=None,
            limit=100,
            offset=0,
        )

    async def test_capability_id_filter_passed_through(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.list.return_value = []

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/providers?capability_id=cap_01HQXR8K9N3JZQP7VW4MEX2YBA")

        assert resp.status_code == 200
        mock_repo.list.assert_called_once_with(
            capability_id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
            enabled_only=False,
            provider_type=None,
            limit=100,
            offset=0,
        )

    async def test_provider_type_filter_passed_through(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.list.return_value = []

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/providers?provider_type=serverless_lb")

        assert resp.status_code == 200
        mock_repo.list.assert_called_once_with(
            capability_id=None,
            enabled_only=False,
            provider_type="serverless_lb",
            limit=100,
            offset=0,
        )


@pytest.mark.anyio
class TestGetProviderHealth:
    async def test_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = _make_provider(health_status="healthy")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/providers/prov_01HQXR8K9N3JZQP7VW4MEX2YBA/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "prov_01HQXR8K9N3JZQP7VW4MEX2YBA"
        assert body["health_status"] == "healthy"
        assert body["recent_error_rate"] == 0.0
        assert body["updated_at"] == "2026-05-28T12:00:00+00:00"

    async def test_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/providers/prov_nonexistent/health")

        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "provider_not_found"
        assert body["id"] == "prov_nonexistent"


@pytest.mark.anyio
class TestPatchProvider:
    async def test_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        original = _make_provider()
        updated = _make_provider(priority=10)
        mock_repo.get.return_value = original
        mock_repo.patch.return_value = updated

        with patch("pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.patch(
                    f"/v1/admin/providers/{original.id}",
                    json={"priority": 10},
                )

        assert resp.status_code == 200
        assert resp.json()["priority"] == 10

    async def test_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.patch(
                "/v1/admin/providers/prov_nonexistent",
                json={"priority": 5},
            )

        assert resp.status_code == 404

    async def test_schema_validation_empty_name_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.patch(
                "/v1/admin/providers/prov_test",
                json={"name": ""},
            )

        assert resp.status_code == 422

    async def test_patch_validates_config_against_existing_provider(self, api_client: tuple):
        app, mock_repo, _ = api_client
        original = _make_provider(
            provider_type=ProviderType.PUBLIC_ENDPOINT,
            runpod_endpoint_id="qwen3-32b-awq",
        )
        mock_repo.get.return_value = original

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.patch(
                f"/v1/admin/providers/{original.id}",
                json={"config": {"openai_base_url": "https://api.runpod.ai/v2/wrong/openai/v1"}},
            )

        assert resp.status_code == 422
        mock_repo.patch.assert_not_called()

    async def test_patch_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        original = _make_provider()
        updated = _make_provider(priority=10)
        mock_repo.get.return_value = original
        mock_repo.patch.return_value = updated

        with patch(
            "pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.patch(
                    f"/v1/admin/providers/{original.id}",
                    json={"priority": 10},
                )

        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["actor"] == "rest:admin"
        assert call_kwargs[1]["action"] == "update"
        assert call_kwargs[1]["entity_type"] == "provider"
        assert call_kwargs[1]["old_value"] is not None
        assert call_kwargs[1]["new_value"] is not None


@pytest.mark.anyio
class TestEnableProvider:
    async def test_enable_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        prov = _make_provider(enabled=False)
        enabled_prov = _make_provider(enabled=True)
        mock_repo.get.return_value = prov
        mock_repo.enable.return_value = enabled_prov

        with patch("pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/providers/{prov.id}/enable")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    async def test_enable_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/providers/prov_nonexistent/enable")

        assert resp.status_code == 404

    async def test_enable_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        prov = _make_provider(enabled=False)
        enabled_prov = _make_provider(enabled=True)
        mock_repo.get.return_value = prov
        mock_repo.enable.return_value = enabled_prov

        with patch(
            "pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/providers/{prov.id}/enable")

        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["action"] == "enable"
        assert call_kwargs[1]["old_value"] == {"enabled": False}
        assert call_kwargs[1]["new_value"] == {"enabled": True}


@pytest.mark.anyio
class TestDisableProvider:
    async def test_disable_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        prov = _make_provider(enabled=True)
        disabled_prov = _make_provider(enabled=False)
        mock_repo.get.return_value = prov
        mock_repo.disable.return_value = disabled_prov

        with patch("pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/providers/{prov.id}/disable")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_disable_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/providers/prov_nonexistent/disable")

        assert resp.status_code == 404

    async def test_disable_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        prov = _make_provider(enabled=True)
        disabled_prov = _make_provider(enabled=False)
        mock_repo.get.return_value = prov
        mock_repo.disable.return_value = disabled_prov

        with patch(
            "pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/providers/{prov.id}/disable")

        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["action"] == "disable"
        assert call_kwargs[1]["old_value"] == {"enabled": True}
        assert call_kwargs[1]["new_value"] == {"enabled": False}


@pytest.mark.anyio
class TestHibernateProvider:
    async def test_hibernate_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        prov = _make_provider(health_status="healthy")
        hibernated = _make_provider(health_status="hibernated")
        mock_repo.get.return_value = prov
        mock_repo.patch.return_value = hibernated

        with patch("pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/providers/{prov.id}/hibernate")

        assert resp.status_code == 200
        body = resp.json()
        assert body["health_status"] == "hibernated"
        assert body["enabled"] is True
        assert "id" in body

    async def test_hibernate_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/providers/prov_nonexistent/hibernate")

        assert resp.status_code == 404

    async def test_hibernate_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        prov = _make_provider(health_status="healthy")
        hibernated = _make_provider(health_status="hibernated")
        mock_repo.get.return_value = prov
        mock_repo.patch.return_value = hibernated

        with patch(
            "pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/providers/{prov.id}/hibernate")

        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["action"] == "hibernate"
        assert call_kwargs[1]["old_value"] == {"health_status": "healthy"}
        assert call_kwargs[1]["new_value"] == {"health_status": "hibernated"}
