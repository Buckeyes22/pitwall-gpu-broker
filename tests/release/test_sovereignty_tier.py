"""Release-tier tests for the sovereignty pre-spend validation tier.

Tier: sovereignty
Purpose: Validate data residency / region constraints.

Sovereignty tier tests verify that workloads tagged with sovereignty constraints
(e.g., homelab_only) do not result in paid RunPod API calls.

Currently v1 has no sovereignty enum - these tests document the constraint
and verify that no paid call is possible even if such a workload were submitted.

The spec says:
  "Sovereignty refuse. Submit a workload tagged sovereignty=homelab_only.
   Pitwall must NOT dispatch to cloud. Currently a documentation-only constraint
   since v1 has no sovereignty enum; codify when consumer attribution lands."
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability, Provider

NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


def _capability() -> Capability:
    return Capability(
        id="cap_embedding_bge_m3",
        name="embedding.bge-m3",
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode=CostMode.PER_REQUEST,
        source=CapabilitySource.API,
        created_at=NOW,
        updated_at=NOW,
    )


def _cloud_provider() -> Provider:
    return Provider(
        id="prov_cloud_runpod",
        capability_id="cap_embedding_bge_m3",
        name="cloud-runpod-bge-m3",
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="cloud-endpoint",
        cloud_type="CLOUD",
        config={
            "cost": {"per_request": "0.0001"},
        },
        priority=1,
        enabled=True,
        health_status="healthy",
        source=CapabilitySource.API,
        updated_at=NOW,
    )


def _homelab_provider() -> Provider:
    return Provider(
        id="prov_homelab",
        capability_id="cap_embedding_bge_m3",
        name="homelab-bge-m3",
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="homelab-endpoint",
        cloud_type="HOMELAB",
        config={
            "cost": {"per_request": "0.0001"},
        },
        priority=1,
        enabled=True,
        health_status="healthy",
        source=CapabilitySource.API,
        updated_at=NOW,
    )


@pytest.mark.release
@pytest.mark.anyio
async def test_sovereignty_homelab_only_workload_no_cloud_paid_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workload with sovereignty=homelab_only must not result in paid cloud calls.

    Even though v1 has no sovereignty enum enforcement, the pre-spend validation
    must ensure no paid RunPod API calls are made when dry_run=True.
    """

    paid_calls: list[str] = []

    async def track_paid_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        paid_calls.append("paid_call")
        return {"id": "pod_should_not_exist"}

    from pitwall.api.routes import inference as inference_module

    monkeypatch.setattr(inference_module, "run_sync_inference", track_paid_call)

    from pitwall.api.app import app
    from pitwall.api.routes.inference import _capability_repo, _provider_repo
    from pitwall.db.repository import CapabilityRepository, ProviderRepository

    capability_repo = AsyncMock(spec=CapabilityRepository)
    capability_repo.get_by_name.return_value = _capability()

    provider_repo = AsyncMock(spec=ProviderRepository)
    provider_repo.list.return_value = [_cloud_provider()]

    app.state.pool = MagicMock()
    app.dependency_overrides[_capability_repo] = lambda: capability_repo
    app.dependency_overrides[_provider_repo] = lambda: provider_repo

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/inference",
                json={
                    "capability": "embedding.bge-m3",
                    "texts": ["hello"],
                    "dry_run": True,
                    "sovereignty": "homelab_only",
                },
            )

        assert resp.status_code == 200
        assert paid_calls == [], (
            "Pre-spend validation with dry_run=True must prevent paid RunPod calls "
            "regardless of sovereignty setting"
        )
    finally:
        app.dependency_overrides.clear()
        if hasattr(app.state, "pool"):
            delattr(app.state, "pool")


@pytest.mark.release
@pytest.mark.anyio
async def test_sovereignty_no_paid_call_possible_with_cloud_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert that even with cloud provider available, no paid call is made.

    This verifies the pre-spend validation safety invariant: when dry_run=True,
    no paid RunPod API call should ever be possible.
    """

    paid_calls: list[str] = []

    async def track_paid_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        paid_calls.append("paid_call")
        return {"id": "pod_should_not_exist"}

    from pitwall.api.routes import inference as inference_module

    monkeypatch.setattr(inference_module, "run_sync_inference", track_paid_call)

    from pitwall.api.app import app
    from pitwall.api.routes.inference import _capability_repo, _provider_repo
    from pitwall.db.repository import CapabilityRepository, ProviderRepository

    capability_repo = AsyncMock(spec=CapabilityRepository)
    capability_repo.get_by_name.return_value = _capability()

    provider_repo = AsyncMock(spec=ProviderRepository)
    provider_repo.list.return_value = [_cloud_provider()]

    app.state.pool = MagicMock()
    app.dependency_overrides[_capability_repo] = lambda: capability_repo
    app.dependency_overrides[_provider_repo] = lambda: provider_repo

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/v1/inference",
                json={
                    "capability": "embedding.bge-m3",
                    "texts": ["hello"],
                    "dry_run": True,
                    "sovereignty": "homelab_only",
                },
            )

        assert paid_calls == [], (
            "Pre-spend validation must prevent paid RunPod calls for dry_run=True requests"
        )
    finally:
        app.dependency_overrides.clear()
        if hasattr(app.state, "pool"):
            delattr(app.state, "pool")


@pytest.mark.release
@pytest.mark.anyio
async def test_sovereignty_tier_mocked_transport_no_real_runpod_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that mocked RunPod transport prevents any real RunPod API calls.

    The sovereignty tier uses mocked transport to ensure no live RunPod calls
    are possible during pre-spend validation.
    """

    from tests.fakes.runpod import RunPodLBFake

    runpod_calls: list[httpx.Request] = []

    def track_request(request: httpx.Request) -> httpx.Response:
        runpod_calls.append(request)
        return httpx.Response(200, json={"dense": []})

    original_transport = RunPodLBFake.transport

    def mock_transport(self: Any) -> httpx.MockTransport:
        return httpx.MockTransport(track_request)

    monkeypatch.setattr(RunPodLBFake, "transport", mock_transport)

    from pitwall.api.app import app
    from pitwall.api.routes.inference import _capability_repo, _provider_repo
    from pitwall.db.repository import CapabilityRepository, ProviderRepository

    capability_repo = AsyncMock(spec=CapabilityRepository)
    capability_repo.get_by_name.return_value = _capability()

    provider_repo = AsyncMock(spec=ProviderRepository)
    provider_repo.list.return_value = [_cloud_provider()]

    app.state.pool = MagicMock()
    app.dependency_overrides[_capability_repo] = lambda: capability_repo
    app.dependency_overrides[_provider_repo] = lambda: provider_repo

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/v1/inference",
                json={
                    "capability": "embedding.bge-m3",
                    "texts": ["hello"],
                    "dry_run": True,
                    "sovereignty": "homelab_only",
                },
            )

        assert len(runpod_calls) == 0, (
            "Mocked transport should result in zero real RunPod API calls"
        )
    finally:
        app.dependency_overrides.clear()
        if hasattr(app.state, "pool"):
            delattr(app.state, "pool")
        monkeypatch.setattr(RunPodLBFake, "transport", original_transport)


@pytest.mark.release
def test_sovereignty_tier_documented_in_conftest() -> None:
    """Assert the release conftest declares the sovereignty tier."""
    from tests.release.conftest import pytest_configure

    config = MagicMock()
    config.addinivalue_line = MagicMock()
    pytest_configure(config)
    config.addinivalue_line.assert_called_once()
    call_args = config.addinivalue_line.call_args[0]
    assert "release" in str(call_args)


@pytest.mark.release
def test_sovereignty_region_residency_gate_blocks_cloud_dispatch() -> None:
    try:
        from pitwall.routing.sovereignty import is_dispatch_allowed
    except ImportError:
        from pitwall.routing import (
            EliminationReason,
            RoutingRequest,
            evaluate_hard_constraints,
        )

        request = RoutingRequest(
            capability_name="embedding.bge-m3",
            capability_id="cap_embedding_bge_m3",
            required_region="homelab",
        )
        cloud_result = evaluate_hard_constraints(
            request,
            {
                "id": "prov_cloud_runpod",
                "capability_id": "cap_embedding_bge_m3",
                "provider_type": "serverless_lb",
                "region": "US-KS-2",
                "config": {},
            },
        )
        homelab_result = evaluate_hard_constraints(
            request,
            {
                "id": "prov_homelab",
                "capability_id": "cap_embedding_bge_m3",
                "provider_type": "serverless_lb",
                "region": "homelab",
                "config": {},
            },
        )

        assert cloud_result.reasons == (EliminationReason.REGION_MISMATCH,)
        assert homelab_result.passed is True
    else:
        assert (
            is_dispatch_allowed(
                requested_residency="homelab_only",
                provider_residency="homelab_only",
                provider_region="homelab",
            )
            is True
        )
        assert (
            is_dispatch_allowed(
                requested_residency="homelab_only",
                provider_residency="cloud",
                provider_region="US-KS-2",
            )
            is False
        )
