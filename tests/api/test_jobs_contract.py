"""Task 5a: async job read/cancel route contract.

Routes: GET /v1/jobs/{id}, /{id}/status, /{id}/result, POST /{id}/cancel.
Verified vs source 2026-05-30:
  - jobs.py handlers call ``_workload_repo(request)`` INLINE (not via Depends),
    so we monkeypatch the module global, not dependency_overrides.
  - Workload requires id, capability_id, provider_id, type, state, submitted_at
    (NOT created_at/updated_at; extras forbidden).
  - get/status/result/cancel -> WorkloadNotFound (404); result -> JobNotReady
    (409) when state is not terminal.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.core.enums import WorkloadState
from pitwall.core.models import Workload
from tests.api._contract_helpers import build_app, client_for

pytestmark = pytest.mark.anyio
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


def _workload(state: WorkloadState) -> Workload:
    return Workload(
        id="wkl_x",
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        type="async",
        state=state,
        result={"ok": True} if state == WorkloadState.COMPLETED else None,
        submitted_at=_NOW,
    )


def _setup(clear_app_module, *, get=None):
    repo = AsyncMock()
    repo.get.return_value = get
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.jobs as jobs_mod

    jobs_mod._workload_repo = lambda request: repo
    return mod, repo


async def test_get_job_unknown_404(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.get("/v1/jobs/wkl_missing")
    assert resp.status_code == 404
    assert resp.json()["error"] == "workload_not_found"


async def test_get_status_unknown_404(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.get("/v1/jobs/wkl_missing/status")
    assert resp.status_code == 404


async def test_get_result_unknown_404(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.get("/v1/jobs/wkl_missing/result")
    assert resp.status_code == 404


async def test_get_result_not_ready_409(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, get=_workload(WorkloadState.RUNNING))
    async with client_for(mod) as client:
        resp = await client.get("/v1/jobs/wkl_x/result")
    assert resp.status_code == 409
    assert resp.json()["error"] == "job_not_ready"


async def test_get_result_completed_200(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, get=_workload(WorkloadState.COMPLETED))
    async with client_for(mod) as client:
        resp = await client.get("/v1/jobs/wkl_x/result")
    assert resp.status_code == 200


async def test_get_job_completed_200(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, get=_workload(WorkloadState.COMPLETED))
    async with client_for(mod) as client:
        resp = await client.get("/v1/jobs/wkl_x")
    assert resp.status_code == 200


async def test_cancel_unknown_404(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, get=None)
    async with client_for(mod) as client:
        resp = await client.post("/v1/jobs/wkl_missing/cancel")
    assert resp.status_code == 404
