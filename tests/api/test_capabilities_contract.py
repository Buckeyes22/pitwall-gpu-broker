"""Task 2a: capabilities route contract.

Routes: POST/PATCH/enable/disable /v1/admin/capabilities* (admin),
GET /v1/capabilities, GET /v1/capabilities/{name} (public). Deps overridden:
capability_routes._repo, ._pool. Verified vs source 2026-05-30: create->201,
duplicate name -> CapabilityConflict (409), unknown id/name -> CapabilityNotFound (404).
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode
from pitwall.core.models import Capability
from tests.api._contract_helpers import build_app, client_for, override

pytestmark = pytest.mark.anyio
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
_ADMIN_SECRET = "test-admin-secret"
_BODY = {
    "name": "embedding.bge-m3",
    "version": "1.0.0",
    "class": "embedding",
    "cost_mode": "per_second",
}


def _cap() -> Capability:
    return Capability(
        id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
        name="embedding.bge-m3",
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode=CostMode.PER_SECOND,
        enabled=True,
        source=CapabilitySource.API,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _setup(clear_app_module, *, get_by_name=None, get=None):
    repo = AsyncMock()
    repo.get_by_name.return_value = get_by_name
    repo.get.return_value = get
    repo.create.return_value = _cap()
    repo.patch.return_value = get
    repo.enable.return_value = get
    repo.disable.return_value = get
    mod = build_app(secret=_ADMIN_SECRET, pool=MagicMock())
    from pitwall.api.capability_routes import _pool as pool_dep
    from pitwall.api.capability_routes import _repo as repo_dep

    override(mod, repo_dep, repo)
    override(mod, pool_dep, MagicMock())
    return mod, repo


async def test_create_happy_201(clear_app_module) -> None:
    mod, repo = _setup(clear_app_module, get_by_name=None)
    with patch("pitwall.api.capability_routes.insert_audit", new=AsyncMock()):
        async with client_for(mod) as client:
            resp = await client.post("/v1/admin/capabilities", json=_BODY)
    assert resp.status_code == 201
    assert resp.json()["name"] == "embedding.bge-m3"


async def test_create_duplicate_409(clear_app_module) -> None:
    mod, repo = _setup(clear_app_module, get_by_name=_cap())
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/capabilities", json=_BODY)
    assert resp.status_code == 409
    assert resp.json()["error"] == "capability_conflict"


async def test_create_missing_required_field_422(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module)
    bad = {k: v for k, v in _BODY.items() if k != "cost_mode"}
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/capabilities", json=bad)
    assert resp.status_code == 422


async def test_create_bad_enum_422(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module)
    bad = {**_BODY, "class": "nonsense"}
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/capabilities", json=bad)
    assert resp.status_code == 422


async def test_get_by_name_404(clear_app_module) -> None:
    mod, repo = _setup(clear_app_module, get_by_name=None)
    async with client_for(mod) as client:
        resp = await client.get("/v1/capabilities/embedding.missing")
    assert resp.status_code == 404
    assert resp.json()["error"] == "capability_not_found"


async def test_patch_unknown_404(clear_app_module) -> None:
    mod, repo = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.patch("/v1/admin/capabilities/cap_missing", json={"version": "2.0.0"})
    assert resp.status_code == 404


async def test_enable_unknown_404(clear_app_module) -> None:
    mod, repo = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/capabilities/cap_missing/enable")
    assert resp.status_code == 404


async def test_disable_unknown_404(clear_app_module) -> None:
    mod, repo = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.post("/v1/admin/capabilities/cap_missing/disable")
    assert resp.status_code == 404


async def test_method_not_allowed_405(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module)
    async with client_for(mod) as client:
        resp = await client.delete("/v1/capabilities/embedding.bge-m3")
    assert resp.status_code == 405
