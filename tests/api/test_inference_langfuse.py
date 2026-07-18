"""Tests for Langfuse trace emission in POST /v1/inference."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

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


def _provider(
    provider_id: str,
    *,
    priority: int,
    health_status: str = "healthy",
    per_second_active: str = "0.0001",
) -> Provider:
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
        config={"per_second_active": per_second_active},
    )


class TestLangfuseFailureDoesNotFailInference:
    @pytest.fixture()
    def api_client_langfuse_failure(self) -> tuple[object, AsyncMock, AsyncMock]:
        old = os.environ.copy()
        os.environ.update(
            {
                "RUNPOD_API_KEY": "test-key",
                "DATABASE_URL": "postgresql://u:p@localhost/db",
                "REDIS_URL": "redis://localhost:6379/0",
                "LANGFUSE_HOST": "http://langfuse.invalid",
                "LANGFUSE_PUBLIC_KEY": "test",
                "LANGFUSE_SECRET_KEY": "test",
            }
        )

        for mod in list(sys.modules):
            if mod.startswith("pitwall.api"):
                del sys.modules[mod]

        from pitwall.api.app import app
        from pitwall.api.routes.inference import _capability_repo, _provider_repo
        from pitwall.observability import langfuse as langfuse_module

        capability_repo = AsyncMock(spec=CapabilityRepository)
        provider_repo = AsyncMock(spec=ProviderRepository)

        async def mock_execute(*args: Any, **kwargs: Any) -> str:
            return None

        async def mock_fetchval(*args: Any, **kwargs: Any) -> Any:
            return None

        async def mock_fetchrow(*args: Any, **kwargs: Any) -> Any:
            return {"s": Decimal("0")}

        mock_conn = MagicMock()
        mock_conn.execute = mock_execute
        mock_conn.fetchval = mock_fetchval
        mock_conn.fetchrow = mock_fetchrow

        class _TransactionCtx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *args):
                pass

        mock_conn.transaction = MagicMock(return_value=_TransactionCtx())

        class _AcquireCtx:
            async def __aenter__(self):
                return mock_conn

            async def __aexit__(self, *args):
                pass

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=_AcquireCtx())

        app.state.pool = mock_pool
        app.dependency_overrides[_capability_repo] = lambda: capability_repo
        app.dependency_overrides[_provider_repo] = lambda: provider_repo

        langfuse_module.reset_client_for_tests()

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
    async def test_langfuse_failure_does_not_fail_inference(
        self,
        api_client_langfuse_failure: tuple[object, AsyncMock, AsyncMock],
    ) -> None:
        app, capability_repo, provider_repo = api_client_langfuse_failure
        capability = _capability()
        capability_repo.get_by_name.return_value = capability
        capability_repo.get.return_value = None
        provider = _provider("prov_1", priority=1)
        provider_repo.list.return_value = [provider]

        with respx.mock(assert_all_called=False, assert_all_mocked=True) as respx_router:
            respx_router.post("https://prov_1-endpoint.api.runpod.ai/embed").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "result_123",
                        "status": "COMPLETED",
                        "dense_embeddings": [[0.1, 0.2, 0.3]],
                    },
                )
            )

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/inference",
                    json={
                        "capability": "embedding.bge-m3",
                        "texts": ["hello"],
                    },
                )

        assert resp.status_code == 200
        body = resp.json()
        assert "workload_id" in body
        assert "result" in body
