"""Hermetic E5 audit checks for ."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pitwall.core.enums import CapabilitySource, ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.cost.sync_gate import SyncInferenceResult
from pitwall.db.repository import CapabilityRepository, ProviderRepository

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_LB_ENDPOINT_ID = "eptest00000000"
_ADMIN_SECRET = "test-admin-secret"
_ADMIN_HEADERS = {"X-Pitwall-Secret": _ADMIN_SECRET}


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


def _provider(
    provider_id: str = "prov_bge_m3_lb",
    *,
    endpoint_id: str = _LB_ENDPOINT_ID,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id="cap_embedding_bge_m3",
        name=provider_id,
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id=endpoint_id,
        priority=1,
        enabled=True,
        health_status="healthy",
        config={"per_second_active": "0.0001"},
        source=CapabilitySource.API,
        updated_at=_NOW,
    )


class _ReplayPool:
    def __init__(self) -> None:
        self.rows_by_key: dict[str, dict[str, Any]] = {}

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(_ReplayConnection(self))

    def remember(
        self,
        idempotency_key: str,
        *,
        workload_id: str,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        self.rows_by_key[idempotency_key] = {
            "id": workload_id,
            "state": "completed",
            "input": payload,
            "result": result,
        }


class _AcquireContext:
    def __init__(self, conn: _ReplayConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> _ReplayConnection:
        return self.conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _ReplayConnection:
    def __init__(self, pool: _ReplayPool) -> None:
        self.pool = pool

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        if "FROM pitwall.workloads" in sql and "idempotency_key" in sql:
            return self.pool.rows_by_key.get(str(args[0]))
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")


@pytest.fixture()
def inference_api_client() -> tuple[object, AsyncMock, AsyncMock, _ReplayPool]:
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
    replay_pool = _ReplayPool()

    app.state.pool = replay_pool
    app.dependency_overrides[_capability_repo] = lambda: capability_repo
    app.dependency_overrides[_provider_repo] = lambda: provider_repo

    yield app, capability_repo, provider_repo, replay_pool

    app.dependency_overrides.clear()
    if hasattr(app.state, "pool"):
        delattr(app.state, "pool")
    os.environ.clear()
    os.environ.update(old)
    for mod in list(sys.modules):
        if mod.startswith("pitwall.api"):
            del sys.modules[mod]


@pytest.fixture()
def provider_api_client() -> tuple[object, AsyncMock, MagicMock]:
    old = os.environ.copy()
    os.environ.update(
        {
            "RUNPOD_API_KEY": "test-key",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "PITWALL_ADMIN_SECRET": _ADMIN_SECRET,
        }
    )

    for mod in list(sys.modules):
        if mod.startswith("pitwall.api"):
            del sys.modules[mod]

    from pitwall.api.app import app
    from pitwall.api.provider_routes import _capability_repo as capability_repo_dep
    from pitwall.api.provider_routes import _pool as pool_dep
    from pitwall.api.provider_routes import _repo as repo_dep

    mock_repo = AsyncMock()
    mock_capability_repo = AsyncMock()
    mock_capability_repo.get.return_value = _capability()
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


@pytest.mark.anyio
async def test_idempotency_key_header_replays_e5_inference_result(
    inference_api_client: tuple[object, AsyncMock, AsyncMock, _ReplayPool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, capability_repo, provider_repo, replay_pool = inference_api_client
    capability_repo.get_by_name.return_value = _capability()
    capability_repo.get.return_value = None
    provider_repo.list.return_value = [_provider()]

    gate_calls: list[str | None] = []

    async def fake_gate_sync_inference(**kwargs: Any) -> SyncInferenceResult:
        idempotency_key = kwargs["idempotency_key"]
        gate_calls.append(idempotency_key)
        result = {
            "dense": [[0.1, 0.2, 0.3]],
            "raw": {"id": "rp_job_1", "status": "COMPLETED"},
        }
        assert isinstance(idempotency_key, str)
        replay_pool.remember(
            idempotency_key,
            workload_id="wkl_replayed",
            payload=kwargs["payload"],
            result=result,
        )
        return SyncInferenceResult(workload_id="wkl_replayed", runpod_result=result)

    monkeypatch.setattr(
        "pitwall.core.inference.gate_sync_inference",
        fake_gate_sync_inference,
    )
    monkeypatch.setattr(
        "pitwall.api.routes.inference.record_inference_trace",
        AsyncMock(return_value=None),
    )

    payload = {"capability": "embedding.bge-m3", "texts": ["hello"]}
    headers = {"Idempotency-Key": "idem-e5-replay"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=_ADMIN_HEADERS,
    ) as client:
        first = await client.post("/v1/inference", json=payload, headers=headers)
        second = await client.post("/v1/inference", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["workload_id"] == "wkl_replayed"
    assert gate_calls == ["idem-e5-replay"]
    provider_repo.list.assert_awaited_once()


@pytest.mark.anyio
async def test_e5_inference_rejects_multi_axis_paid_launch_fields(
    inference_api_client: tuple[object, AsyncMock, AsyncMock, _ReplayPool],
) -> None:
    app, capability_repo, provider_repo, _ = inference_api_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=_ADMIN_HEADERS,
    ) as client:
        response = await client.post(
            "/v1/inference",
            json={
                "capability": "embedding.bge-m3",
                "texts": ["hello"],
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
    capability_repo.get_by_name.assert_not_called()
    provider_repo.list.assert_not_called()


@pytest.mark.anyio
async def test_l5_provider_registry_rejects_lb_without_existing_endpoint_id(
    provider_api_client: tuple[object, AsyncMock, MagicMock],
) -> None:
    app, mock_repo, _ = provider_api_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=_ADMIN_HEADERS,
    ) as client:
        response = await client.post(
            "/v1/admin/providers",
            json={
                "capability_id": "cap_embedding_bge_m3",
                "name": "bge-m3-lb-us-ks",
                "provider_type": "serverless_lb",
            },
        )

    assert response.status_code == 422
    assert "existing runpod_endpoint_id" in str(response.json()["detail"])
    mock_repo.get.assert_not_called()
    mock_repo.create.assert_not_called()


@pytest.mark.anyio
async def test_l5_provider_registry_registers_existing_lb_endpoint(
    provider_api_client: tuple[object, AsyncMock, MagicMock],
) -> None:
    app, mock_repo, _ = provider_api_client
    mock_repo.get.return_value = None
    mock_repo.create.return_value = _provider(endpoint_id=_LB_ENDPOINT_ID)

    with patch("pitwall.api.provider_routes.insert_audit", new_callable=AsyncMock):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=_ADMIN_HEADERS,
        ) as client:
            response = await client.post(
                "/v1/admin/providers",
                json={
                    "capability_id": "cap_embedding_bge_m3",
                    "name": "bge-m3-lb-us-ks",
                    "provider_type": "serverless_lb",
                    "runpod_endpoint_id": _LB_ENDPOINT_ID,
                },
            )

    assert response.status_code == 201
    assert response.json()["runpod_endpoint_id"] == _LB_ENDPOINT_ID
    created_provider = mock_repo.create.await_args.args[0]
    assert created_provider.runpod_endpoint_id == _LB_ENDPOINT_ID
