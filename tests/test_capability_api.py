"""API tests for the Capability CRUD surface.

Covers happy paths, duplicate name rejection, schema validation,
enable/disable toggling, and config audit row creation.
All tests are hermetic — no database or external service required.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pitwall.core.enums import (
    CapabilityClass,
    CapabilityHint,
    CapabilitySource,
    CostMode,
    ResultDelivery,
)
from pitwall.core.models import Capability, CapabilityDefaults
from pitwall.db.repository import CapabilityRepository

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
ADMIN_SECRET = "test-admin-secret"
ADMIN_HEADERS = {"X-Pitwall-Secret": ADMIN_SECRET}


def _make_capability(
    id: str = "cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
    name: str = "embedding.bge-m3",
    version: str = "1.0.0",
    class_: CapabilityClass = CapabilityClass.EMBEDDING,
    cost_mode: CostMode = CostMode.PER_SECOND,
    enabled: bool = True,
    description: str | None = None,
    input_schema: dict[str, object] | None = None,
    output_schema: dict[str, object] | None = None,
    defaults: CapabilityDefaults | None = None,
    hints_supported: list[CapabilityHint] | None = None,
    source: CapabilitySource = CapabilitySource.API,
) -> Capability:
    return Capability(
        id=id,
        name=name,
        version=version,
        class_=class_,
        description=description,
        input_schema=input_schema or {},
        output_schema=output_schema or {},
        defaults=defaults or CapabilityDefaults(),
        cost_mode=cost_mode,
        hints_supported=hints_supported or [],
        source=source,
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _base_create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "embedding.bge-m3",
        "version": "1.0.0",
        "class": "embedding",
        "cost_mode": "per_second",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def mock_repo() -> AsyncMock:
    return AsyncMock(spec=CapabilityRepository)


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
    from pitwall.api.capability_routes import _pool as pool_dep
    from pitwall.api.capability_routes import _repo as repo_dep

    mock_pool = MagicMock()
    app.dependency_overrides[repo_dep] = lambda: mock_repo
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
class TestCreateCapability:
    async def test_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get_by_name.return_value = None
        mock_repo.create.return_value = _make_capability()

        with patch("pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post("/v1/admin/capabilities", json=VALID_CREATE)

        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "embedding.bge-m3"
        assert body["class"] == "embedding"
        assert body["cost_mode"] == "per_second"
        assert body["enabled"] is True
        assert "id" in body
        assert "created_at" in body

    async def test_with_all_optional_fields(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get_by_name.return_value = None
        mock_repo.create.return_value = _make_capability(
            description="BGE-M3 dense embeddings",
            input_schema={"type": "object"},
            output_schema={"type": "array"},
            defaults=CapabilityDefaults(
                execution_timeout_ms=30_000,
                ttl_ms=120_000,
                result_delivery=ResultDelivery.ASYNC,
            ),
            hints_supported=[CapabilityHint.LATENCY_SENSITIVE],
            source=CapabilitySource.YAML,
        )

        payload = _base_create_payload(
            description="BGE-M3 dense embeddings",
            input_schema={"type": "object"},
            output_schema={"type": "array"},
            defaults={
                "execution_timeout_ms": 30_000,
                "ttl_ms": 120_000,
                "result_delivery": "async",
            },
            hints_supported=["latency_sensitive"],
            source="yaml",
        )

        with patch("pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post("/v1/admin/capabilities", json=payload)

        assert resp.status_code == 201
        body = resp.json()
        assert body["description"] == "BGE-M3 dense embeddings"
        assert body["defaults"]["result_delivery"] == "async"
        assert body["hints_supported"] == ["latency_sensitive"]
        assert body["source"] == "yaml"

    async def test_duplicate_name_returns_409(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get_by_name.return_value = _make_capability()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/capabilities", json=VALID_CREATE)

        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "capability_conflict"
        assert body["name"] == "embedding.bge-m3"

    async def test_schema_validation_missing_required_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/capabilities", json={})

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        missing = {e["loc"][-1] for e in detail}
        assert "name" in missing
        assert "version" in missing

    async def test_schema_validation_empty_name_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post(
                "/v1/admin/capabilities",
                json=_base_create_payload(name=""),
            )

        assert resp.status_code == 422

    async def test_schema_validation_invalid_class_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post(
                "/v1/admin/capabilities",
                json=_base_create_payload(**{"class": "not_a_real_class"}),
            )

        assert resp.status_code == 422

    async def test_schema_validation_invalid_cost_mode_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post(
                "/v1/admin/capabilities",
                json=_base_create_payload(cost_mode="bogus"),
            )

        assert resp.status_code == 422

    async def test_create_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        mock_repo.get_by_name.return_value = None
        mock_repo.create.return_value = _make_capability()

        with patch(
            "pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post("/v1/admin/capabilities", json=VALID_CREATE)

        assert resp.status_code == 201
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["actor"] == "rest:admin"
        assert call_kwargs[1]["action"] == "create"
        assert call_kwargs[1]["entity_type"] == "capability"
        assert call_kwargs[1]["new_value"] is not None


@pytest.mark.anyio
class TestListCapabilities:
    async def test_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.list.return_value = [
            _make_capability(name="cap-a"),
            _make_capability(id="cap_02ANOTHERULID0000000000", name="cap-b"),
        ]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/capabilities")

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
            resp = await client.get("/v1/capabilities")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []

    async def test_enabled_filter_passes_enabled_only_true(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.list.return_value = [_make_capability()]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/capabilities?enabled=true")

        assert resp.status_code == 200
        mock_repo.list.assert_called_once_with(
            enabled_only=True,
            class_filter=None,
            limit=100,
            offset=0,
        )

    async def test_class_filter_passed_through(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.list.return_value = []

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/capabilities?class=llm")

        assert resp.status_code == 200
        mock_repo.list.assert_called_once_with(
            enabled_only=False,
            class_filter="llm",
            limit=100,
            offset=0,
        )


@pytest.mark.anyio
class TestGetCapability:
    async def test_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get_by_name.return_value = _make_capability()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/capabilities/embedding.bge-m3")

        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "embedding.bge-m3"
        assert body["enabled"] is True

    async def test_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get_by_name.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.get("/v1/capabilities/nonexistent")

        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "capability_not_found"
        assert body["name"] == "nonexistent"


@pytest.mark.anyio
class TestPatchCapability:
    async def test_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        original = _make_capability()
        updated = _make_capability(description="updated desc")
        mock_repo.get.return_value = original
        mock_repo.patch.return_value = updated

        with patch("pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.patch(
                    f"/v1/admin/capabilities/{original.id}",
                    json={"description": "updated desc"},
                )

        assert resp.status_code == 200
        assert resp.json()["description"] == "updated desc"

    async def test_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.patch(
                "/v1/admin/capabilities/cap_nonexistent",
                json={"description": "x"},
            )

        assert resp.status_code == 404

    async def test_schema_validation_empty_name_returns_422(self, api_client: tuple):
        app, _, _ = api_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.patch(
                "/v1/admin/capabilities/cap_test",
                json={"name": ""},
            )

        assert resp.status_code == 422

    async def test_patch_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        original = _make_capability()
        updated = _make_capability(description="new desc")
        mock_repo.get.return_value = original
        mock_repo.patch.return_value = updated

        with patch(
            "pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.patch(
                    f"/v1/admin/capabilities/{original.id}",
                    json={"description": "new desc"},
                )

        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["actor"] == "rest:admin"
        assert call_kwargs[1]["action"] == "update"
        assert call_kwargs[1]["entity_type"] == "capability"
        assert call_kwargs[1]["old_value"] is not None
        assert call_kwargs[1]["new_value"] is not None


@pytest.mark.anyio
class TestEnableCapability:
    async def test_enable_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        cap = _make_capability(enabled=False)
        enabled_cap = _make_capability(enabled=True)
        mock_repo.get.return_value = cap
        mock_repo.enable.return_value = enabled_cap

        with patch("pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/capabilities/{cap.id}/enable")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    async def test_enable_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/capabilities/cap_nonexistent/enable")

        assert resp.status_code == 404

    async def test_enable_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        cap = _make_capability(enabled=False)
        enabled_cap = _make_capability(enabled=True)
        mock_repo.get.return_value = cap
        mock_repo.enable.return_value = enabled_cap

        with patch(
            "pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/capabilities/{cap.id}/enable")

        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["action"] == "enable"
        assert call_kwargs[1]["old_value"] == {"enabled": False}
        assert call_kwargs[1]["new_value"] == {"enabled": True}


@pytest.mark.anyio
class TestDisableCapability:
    async def test_disable_happy_path(self, api_client: tuple):
        app, mock_repo, _ = api_client
        cap = _make_capability(enabled=True)
        disabled_cap = _make_capability(enabled=False)
        mock_repo.get.return_value = cap
        mock_repo.disable.return_value = disabled_cap

        with patch("pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/capabilities/{cap.id}/disable")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_disable_not_found_returns_404(self, api_client: tuple):
        app, mock_repo, _ = api_client
        mock_repo.get.return_value = None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
        ) as client:
            resp = await client.post("/v1/admin/capabilities/cap_nonexistent/disable")

        assert resp.status_code == 404

    async def test_disable_writes_audit_row(self, api_client: tuple):
        app, mock_repo, mock_pool = api_client
        cap = _make_capability(enabled=True)
        disabled_cap = _make_capability(enabled=False)
        mock_repo.get.return_value = cap
        mock_repo.disable.return_value = disabled_cap

        with patch(
            "pitwall.api.capability_routes.insert_audit", new_callable=AsyncMock
        ) as mock_audit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=ADMIN_HEADERS,
            ) as client:
                resp = await client.post(f"/v1/admin/capabilities/{cap.id}/disable")

        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["action"] == "disable"
        assert call_kwargs[1]["old_value"] == {"enabled": True}
        assert call_kwargs[1]["new_value"] == {"enabled": False}
