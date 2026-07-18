"""Tests for E5 Stage 1+2 capability routing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.resolver import NoHealthyProviderError, resolve_capability
from pitwall.resolver.service import select_stage12_provider
from pitwall.routing import PlanningContext, RoutingRequest

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _capability(
    *,
    cap_id: str = "cap_embedding_bge_m3",
    name: str = "embedding.bge-m3",
    enabled: bool = True,
) -> Capability:
    return Capability(
        id=cap_id,
        name=name,
        version="1.0.0",
        class_="embedding",
        cost_mode="per_second",
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    provider_id: str,
    *,
    capability_id: str = "cap_embedding_bge_m3",
    priority: int = 1,
    enabled: bool = True,
    health_status: str = "healthy",
    cooldown_until: datetime | None = None,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id=capability_id,
        name=provider_id,
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id=f"{provider_id}-endpoint",
        priority=priority,
        enabled=enabled,
        health_status=health_status,
        cooldown_until=cooldown_until,
        updated_at=_NOW,
    )


class FakeCapabilityRepo:
    def __init__(self, capability: Capability | None) -> None:
        self.capability = capability

    async def get_by_name(self, name: str) -> Capability | None:
        if self.capability is not None and self.capability.name == name:
            return self.capability
        return None

    async def get(self, capability_id: str) -> Capability | None:
        if self.capability is not None and self.capability.id == capability_id:
            return self.capability
        return None


class FakeProviderRepo:
    def __init__(self, providers: list[Provider]) -> None:
        self.providers = providers
        self.list_calls: list[dict[str, object]] = []

    async def get(self, provider_id: str) -> Provider | None:
        for provider in self.providers:
            if provider.id == provider_id:
                return provider
        return None

    async def list(
        self,
        *,
        capability_id: str | None = None,
        enabled_only: bool = False,
        provider_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Provider]:
        self.list_calls.append(
            {
                "capability_id": capability_id,
                "enabled_only": enabled_only,
                "provider_type": provider_type,
                "limit": limit,
                "offset": offset,
            }
        )
        result = [
            provider
            for provider in self.providers
            if capability_id is None or provider.capability_id == capability_id
        ]
        if enabled_only:
            result = [provider for provider in result if provider.enabled]
        return result[:limit]


@pytest.mark.anyio
async def test_resolve_capability_selects_priority_one_enabled_healthy_provider() -> None:
    capability = _capability()
    provider_repo = FakeProviderRepo(
        [
            _provider("prov_priority_2", priority=2),
            _provider("prov_priority_1", priority=1),
        ]
    )

    resolution = await resolve_capability(
        "embedding.bge-m3",
        capability_repo=FakeCapabilityRepo(capability),
        provider_repo=provider_repo,
        now=_NOW,
    )

    assert resolution.provider.id == "prov_priority_1"
    assert provider_repo.list_calls == [
        {
            "capability_id": "cap_embedding_bge_m3",
            "enabled_only": True,
            "provider_type": None,
            "limit": 100,
            "offset": 0,
        }
    ]


def test_stage12_filters_disabled_unhealthy_unknown_and_cooling_providers() -> None:
    capability = _capability()
    request = RoutingRequest(capability_name="embedding.bge-m3")
    resolution = select_stage12_provider(
        request,
        [
            _provider("prov_disabled", priority=1, enabled=False),
            _provider("prov_unhealthy", priority=1, health_status="unhealthy"),
            _provider("prov_unknown", priority=1, health_status="unknown"),
            _provider(
                "prov_cooling",
                priority=1,
                cooldown_until=_NOW + timedelta(minutes=5),
            ),
            _provider("prov_selected", priority=2),
        ],
        capability=capability,
        now=_NOW,
    )

    assert resolution.provider.id == "prov_selected"
    assert resolution.eligible_providers == (_provider("prov_selected", priority=2),)
    assert resolution.to_dict()["selected_provider_id"] == "prov_selected"
    assert {item.provider_id for item in resolution.eliminated} == {
        "prov_disabled",
        "prov_unhealthy",
        "prov_unknown",
        "prov_cooling",
    }


@pytest.mark.anyio
async def test_resolve_capability_raises_when_no_enabled_healthy_provider_survives() -> None:
    with pytest.raises(NoHealthyProviderError):
        await resolve_capability(
            "embedding.bge-m3",
            capability_repo=FakeCapabilityRepo(_capability()),
            provider_repo=FakeProviderRepo(
                [_provider("prov_unhealthy", health_status="unhealthy")]
            ),
            now=_NOW,
        )


@pytest.mark.anyio
async def test_resolve_capability_uses_planning_context_now_for_cooldown() -> None:
    resolution = await resolve_capability(
        "embedding.bge-m3",
        capability_repo=FakeCapabilityRepo(_capability()),
        provider_repo=FakeProviderRepo(
            [
                _provider(
                    "prov_cooling",
                    priority=1,
                    cooldown_until=_NOW + timedelta(minutes=5),
                ),
                _provider("prov_selected", priority=2),
            ]
        ),
        context=PlanningContext.replay(now=_NOW),
    )

    assert resolution.provider.id == "prov_selected"
