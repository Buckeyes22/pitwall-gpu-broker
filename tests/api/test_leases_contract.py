"""Task 5b: lease route contract (hermetic).

Routes: POST /v1/leases, GET/PATCH /v1/leases/{id}, POST /{id}/renew.
Deps via Depends (overridable): _lease_repo, _capability_repo, _provider_repo.

Verified vs source 2026-05-30:
  - create_lease: capability_repo.get_by_name(None) -> LeaseNotFound (404)
  - get/renew unknown id -> LeaseNotFound (404)
  - patch_lease checks lease_patch_conflicting_fields(raw_body) FIRST; a body
    spanning >=2 change-set axes (image / gpu / volume — see schemas/leases.py
    _CHANGE_SET_AXES) -> ChangeSetTooBroad (400), before the repo lookup.
  - accepted-but-unsupported compatibility fields are rejected with a stable
    422 before repository access.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.core.enums import LeaseRenewalPolicy, LeaseState
from pitwall.core.models import Lease, LeaseEndpoints, LeaseReadiness
from pitwall.db.repository import LeaseMutationResult
from tests.api._contract_helpers import build_app, client_for, override

pytestmark = pytest.mark.anyio


def _lease_repo_app(clear_app_module, *, get=None):
    repo = AsyncMock()
    repo.get.return_value = get
    repo.renew.return_value = None if get is None else LeaseMutationResult(get)
    repo.patch_settings.return_value = None if get is None else LeaseMutationResult(get)
    mod = build_app(pool=MagicMock())
    from pitwall.api.routes.leases import _lease_repo

    override(mod, _lease_repo, repo)
    return mod, repo


def _create_app(clear_app_module, *, capability=None, provider=None):
    cap_repo = AsyncMock()
    cap_repo.get_by_name.return_value = capability
    prov_repo = AsyncMock()
    prov_repo.get.return_value = provider
    mod = build_app(pool=MagicMock())
    from pitwall.api.routes.leases import _capability_repo, _provider_repo

    override(mod, _capability_repo, cap_repo)
    override(mod, _provider_repo, prov_repo)
    return mod


async def test_create_unknown_capability_404(clear_app_module) -> None:
    mod = _create_app(clear_app_module, capability=None)
    async with client_for(mod) as client:
        resp = await client.post("/v1/leases", json={"capability_id": "missing.cap"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "lease_not_found"


async def test_create_rejects_null_byte_capability_id_before_db(clear_app_module) -> None:
    cap_repo = AsyncMock()
    mod = build_app(pool=MagicMock())
    from pitwall.api.routes.leases import _capability_repo

    override(mod, _capability_repo, cap_repo)

    async with client_for(mod) as client:
        resp = await client.post("/v1/leases", json={"capability_id": "\x00"})

    assert resp.status_code == 422
    cap_repo.get_by_name.assert_not_awaited()


async def test_create_lease_success_returns_lease_response(clear_app_module, monkeypatch) -> None:
    """A real (non-dry-run) launch must shape its 201 body as LeaseResponse.

    Regression for the create_lease response-shape bug: the route returned the
    raw run_launch result dict (backend/pod_id/lease_id/...) instead of a
    LeaseResponse, so every successful pod-lease 500'd on response validation
    (observed live, once a lease could finally reach ACTIVE).
    """
    import pitwall.api.routes.leases as leases_mod
    from pitwall.api.routes.leases import _lease_repo

    created = dt.datetime(2026, 5, 31, 12, 0, 0, tzinfo=dt.UTC)
    signal = created + dt.timedelta(seconds=30)
    lease = Lease(
        id="lease_ok",
        provider_id="prov_x",
        runpod_pod_id="pod_ok",
        state=LeaseState.ACTIVE,
        created_at=created,
        expires_at=created + dt.timedelta(hours=1),
        renewal_policy=LeaseRenewalPolicy.MANUAL,
        endpoints=LeaseEndpoints(http={"80": "https://pod-80.proxy.runpod.net"}),
        readiness=LeaseReadiness(
            runtime_seen_at=signal,
            port_mappings_seen_at=signal,
            probe_passed_at=signal,
            probe_method="runpod_proxy",
        ),
    )

    mod = _create_app(clear_app_module, capability=MagicMock(), provider=MagicMock())
    monkeypatch.setattr(
        leases_mod,
        "run_launch",
        AsyncMock(return_value={"lease_id": "lease_ok", "pod_id": "pod_ok", "backend": "runpod"}),
    )
    lease_repo = AsyncMock()
    lease_repo.get.return_value = lease
    override(mod, _lease_repo, lease_repo)

    async with client_for(mod) as client:
        resp = await client.post(
            "/v1/leases", json={"capability_id": "pod.x", "provider_id": "prov_x"}
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == "lease_ok"
    assert body["runpod_pod_id"] == "pod_ok"
    assert body["state"] == "active"
    assert body["provider_id"] == "prov_x"
    lease_repo.get.assert_awaited_once_with("lease_ok")


async def test_get_unknown_404(clear_app_module) -> None:
    mod, _ = _lease_repo_app(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.get("/v1/leases/lease_missing")
    assert resp.status_code == 404
    assert resp.json()["error"] == "lease_not_found"


async def test_renew_unknown_404(clear_app_module) -> None:
    mod, _ = _lease_repo_app(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.post("/v1/leases/lease_missing/renew", json={"extends_minutes": 30})
    assert resp.status_code == 404


async def test_patch_change_set_too_broad_400(clear_app_module) -> None:
    # image axis (image) + gpu axis (gpu_class) => spans 2 change-set axes.
    mod, _ = _lease_repo_app(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.patch(
            "/v1/leases/lease_x", json={"image": "img:v2", "gpu_class": "NVIDIA L4"}
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "change_set_too_broad"


async def test_patch_single_axis_unsupported_422_before_repo(clear_app_module) -> None:
    mod, repo = _lease_repo_app(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.patch("/v1/leases/lease_missing", json={"image": "img:v2"})
    assert resp.status_code == 422
    assert resp.json() == {"error": "unsupported_lease_patch", "fields": ["image"]}
    repo.patch_settings.assert_not_awaited()
