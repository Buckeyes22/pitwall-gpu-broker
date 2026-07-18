from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability, CapabilityDefaults, Provider

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)


def _capability() -> Capability:
    return Capability(
        id="cap_embedding_demo",
        name="embedding.demo",
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        description="Demo embedding capability",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        defaults=CapabilityDefaults(),
        cost_mode=CostMode.PER_SECOND,
        hints_supported=[],
        source=CapabilitySource.YAML,
        last_applied_yaml_hash="hash-cap",
        enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(*, enabled: bool = True) -> Provider:
    return Provider(
        id="prov_embedding_demo",
        capability_id="cap_embedding_demo",
        name="embedding-demo-lb",
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="eptest00000000",
        runpod_template_id=None,
        region="US-KS-2",
        cloud_type=None,
        config={"lb_base_url": "https://eptest00000000.api.runpod.ai"},
        priority=10,
        enabled=enabled,
        health_status="healthy",
        consecutive_failures=0,
        cooldown_trips=0,
        cold_start_p50_ms=8000,
        cold_start_p95_ms=22000,
        recent_error_rate=0.0,
        cooldown_until=None,
        source=CapabilitySource.YAML,
        last_applied_yaml_hash="hash-provider",
        updated_at=_NOW,
    )


class _CapabilityRepo:
    def __init__(self, pool: object) -> None:
        self.pool = pool

    async def list(
        self,
        *,
        enabled_only: bool = False,
        class_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Capability]:
        return [_capability()]


class _ProviderRepo:
    def __init__(self, pool: object, *, enabled: bool = True) -> None:
        self.pool = pool
        self.enabled = enabled
        self.created: list[Provider] = []

    async def list(
        self,
        *,
        capability_id: str | None = None,
        enabled_only: bool = False,
        provider_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Provider]:
        return [_provider(enabled=self.enabled)]

    async def create(self, provider: Provider) -> Provider:
        self.created.append(provider)
        return provider


@pytest.mark.anyio
async def test_copilot_disable_provider_returns_proposal_only_gitops_plan() -> None:
    from pitwall.mcp.tools.copilot import pitwall_copilot_propose

    provider_repo = _ProviderRepo(object())

    with (
        patch("pitwall.mcp.tools.copilot.get_pool", AsyncMock(return_value=object())),
        patch("pitwall.mcp.tools.copilot.CapabilityRepository", _CapabilityRepo),
        patch(
            "pitwall.mcp.tools.copilot.ProviderRepository",
            lambda pool: provider_repo,
        ),
    ):
        result = await pitwall_copilot_propose("disable provider embedding-demo-lb")

    assert result["proposal_only"] is True
    assert result["applied"] is False
    assert provider_repo.created == []

    assert result["plan"]["counts"] == {"create": 0, "update": 1, "delete": 0}
    assert result["diff"]["has_destructive_changes"] is False

    operation = result["plan"]["operations"][0]
    assert operation["action"] == "update"
    assert operation["entity_type"] == "provider"
    assert operation["entity_id"] == "prov_embedding_demo"
    assert operation["changes"]["enabled"] == {"current": True, "desired": False}

    assert result["desired_state"]["providers"][0]["enabled"] is False
    assert "disable provider embedding-demo-lb" in result["rationale"][0]


@pytest.mark.anyio
async def test_copilot_can_follow_recommendation_engine_drift_signal() -> None:
    from pitwall.mcp.tools.copilot import pitwall_copilot_propose

    drift_findings: list[dict[str, Any]] = [
        {
            "provider_id": "prov_embedding_demo",
            "field": "enabled",
            "expected": False,
            "observed": True,
            "severity": "high",
            "message": "Provider is disabled but still running",
        }
    ]

    with (
        patch("pitwall.mcp.tools.copilot.get_pool", AsyncMock(return_value=object())),
        patch("pitwall.mcp.tools.copilot.CapabilityRepository", _CapabilityRepo),
        patch("pitwall.mcp.tools.copilot.ProviderRepository", _ProviderRepo),
    ):
        result = await pitwall_copilot_propose(
            "follow top recommendation",
            drift_findings=drift_findings,
        )

    operation = result["plan"]["operations"][0]
    assert operation["changes"]["enabled"] == {"current": True, "desired": False}
    assert result["recommendations"][0]["action"] == "disable_or_investigate_running_provider"
    assert result["rationale"][0].startswith("RecommendationEngine selected")


@pytest.mark.anyio
async def test_copilot_rejects_unsupported_intent_without_mutation() -> None:
    from pitwall.mcp.tools.copilot import pitwall_copilot_propose

    with (
        patch("pitwall.mcp.tools.copilot.get_pool", AsyncMock(return_value=object())),
        patch("pitwall.mcp.tools.copilot.CapabilityRepository", _CapabilityRepo),
        patch("pitwall.mcp.tools.copilot.ProviderRepository", _ProviderRepo),
        pytest.raises(ValueError, match="unsupported copilot intent"),
    ):
        await pitwall_copilot_propose("delete every provider")


def test_copilot_tool_is_registered() -> None:
    from pitwall.mcp.registry import TOOL_NAMES, TOOL_REGISTRY
    from pitwall.mcp.tools.copilot import pitwall_copilot_propose

    assert "pitwall_copilot_propose" in TOOL_NAMES
    spec = next(item for item in TOOL_REGISTRY if item.name == "pitwall_copilot_propose")
    assert spec.handler is pitwall_copilot_propose
    assert "proposal-only" in spec.description
