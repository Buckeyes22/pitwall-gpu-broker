"""Task 2b: providers route contract.

Routes: POST/PATCH/enable/disable/hibernate /v1/admin/providers* (admin),
GET /v1/providers, /v1/providers/{id}, /v1/providers/{id}/health (public).
Deps overridden: provider_routes._repo, ._pool. Verified vs source 2026-05-30:
create_provider detects a duplicate via repo.get(body.name) -> ProviderConflict
(409); there is NO capability check on create (no _capability_repo dep). Unknown
provider id on get/patch/enable/disable/hibernate/health -> ProviderNotFound (404).
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from tests.api._contract_helpers import build_app, client_for, override

pytestmark = pytest.mark.anyio
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
_ADMIN_SECRET = "test-admin-secret"


def _provider_body(**over):
    body = {
        "name": "bge-m3-lb-us-ks",
        "capability_id": "cap_bge_m3",
        "provider_type": "serverless_lb",
        "runpod_endpoint_id": "eptest00000000",
        "priority": 1,
    }
    body.update(over)
    return body


def _provider() -> Provider:
    return Provider(
        id="prov_bge_m3",
        capability_id="cap_bge_m3",
        name="bge-m3-lb-us-ks",
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="eptest00000000",
        config={"lb_base_url": "https://eptest00000000.api.runpod.ai"},
        priority=1,
        enabled=True,
        health_status="healthy",
        updated_at=_NOW,
    )


_ANY_CAPABILITY = object()


def _setup(clear_app_module, *, get=None, capability=_ANY_CAPABILITY):
    """get is the value repo.get(...) returns (used for both conflict + lookups)."""
    repo = AsyncMock()
    repo.get.return_value = get
    repo.create.return_value = _provider()
    repo.patch.return_value = get
    repo.enable.return_value = get
    repo.disable.return_value = get
    capability_repo = AsyncMock()
    capability_repo.get.return_value = capability
    mod = build_app(secret=_ADMIN_SECRET, pool=MagicMock())
    from pitwall.api.provider_routes import _capability_repo as capability_repo_dep
    from pitwall.api.provider_routes import _pool as pool_dep
    from pitwall.api.provider_routes import _repo as repo_dep

    override(mod, repo_dep, repo)
    override(mod, capability_repo_dep, capability_repo)
    override(mod, pool_dep, MagicMock())
    return mod, repo, capability_repo


async def test_create_happy_201(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    with patch("pitwall.api.provider_routes.insert_audit", new=AsyncMock()):
        async with client_for(mod) as client:
            resp = await client.post("/v1/admin/providers", json=_provider_body())
    assert resp.status_code == 201
    assert resp.json()["name"] == "bge-m3-lb-us-ks"


async def test_create_duplicate_409(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=_provider())
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/providers", json=_provider_body())
    assert resp.status_code == 409
    assert resp.json()["error"] == "provider_conflict"


async def test_create_missing_capability_422_friendly(clear_app_module) -> None:
    mod, repo, _ = _setup(clear_app_module, get=None, capability=None)
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/providers", json=_provider_body())
    body = resp.json()
    assert resp.status_code == 422
    assert body["error"] == "provider_capability_missing"
    assert body["capability_id"] == "cap_bge_m3"
    assert "create it first" in body["message"]
    repo.create.assert_not_awaited()


async def test_create_missing_field_422(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    bad = {k: v for k, v in _provider_body().items() if k != "capability_id"}
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/providers", json=bad)
    assert resp.status_code == 422


async def test_create_bad_enum_422(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.post(
            "/v1/admin/providers", json=_provider_body(provider_type="nonsense")
        )
    assert resp.status_code == 422


async def test_get_unknown_404(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.get("/v1/providers/prov_missing")
    assert resp.status_code == 404
    assert resp.json()["error"] == "provider_not_found"


async def test_patch_unknown_404(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.patch("/v1/admin/providers/prov_missing", json={"priority": 2})
    assert resp.status_code == 404


async def test_enable_unknown_404(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/providers/prov_missing/enable")
    assert resp.status_code == 404


async def test_disable_unknown_404(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/providers/prov_missing/disable")
    assert resp.status_code == 404


async def test_get_health_unknown_404(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.get("/v1/providers/prov_missing/health")
    assert resp.status_code == 404


async def test_method_not_allowed_405(clear_app_module) -> None:
    mod, _, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.delete("/v1/providers/prov_bge_m3")
    assert resp.status_code == 405
