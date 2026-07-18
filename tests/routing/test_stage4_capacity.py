"""Tests for Stage 4 pod-lease capacity integration — ."""

from __future__ import annotations

from datetime import UTC, datetime

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.routing import ProviderEliminated, RoutingRequest, plan_route
from pitwall.runpod_client.availability import AvailabilityCache

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _capability() -> Capability:
    return Capability(
        id="cap_vision",
        name="vision.yolov11",
        version="1.0.0",
        **{"class": "vision"},
        cost_mode="per_second",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    provider_id: str,
    *,
    provider_type: ProviderType,
    priority: int = 1,
    config: dict[str, object] | None = None,
    region: str | None = "US-KS-2",
    cloud_type: str | None = None,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id="cap_vision",
        name=provider_id,
        provider_type=provider_type,
        region=region,
        cloud_type=cloud_type,
        config=config or {},
        priority=priority,
        health_status="healthy",
        updated_at=_NOW,
    )


def _pod_provider(
    provider_id: str,
    *,
    priority: int = 1,
    **config: object,
) -> Provider:
    merged = {
        "gpu_type_priority": ["NVIDIA L4"],
        "gpu_count": 1,
    }
    merged.update(config)
    return _provider(
        provider_id,
        provider_type=ProviderType.POD_LEASE,
        priority=priority,
        config=merged,
        cloud_type="SECURE",
    )


def test_stage4_eliminates_unavailable_pod_lease_and_uses_fallback() -> None:
    cache = AvailabilityCache()
    cache.set_available("US-KS-2", "NVIDIA L4", "SECURE", 1, available=False)
    request = RoutingRequest(capability_name="vision.yolov11")
    providers = [
        _pod_provider(
            "prov_pod",
            fallback_chain=["prov_public"],
        ),
        _provider(
            "prov_public",
            provider_type=ProviderType.PUBLIC_ENDPOINT,
            priority=2,
            config={"fallback_for": ["prov_pod"]},
        ),
    ]

    plan = plan_route(
        request,
        providers,
        capability=_capability(),
        now=_NOW,
        availability_cache=cache,
    )

    assert plan.selected_provider_id == "prov_public"
    assert plan.fallback_chain == ("prov_public",)
    assert plan.dropped_provider_reasons == {
        "prov_pod": [ProviderEliminated.CAPACITY_UNAVAILABLE.value],
    }
    assert plan.eliminated[0].stage == 4
    assert plan.capacity_decisions[0].to_dict() == {
        "provider_id": "prov_pod",
        "stage": 4,
        "checked": True,
        "available": False,
        "reason": "capacity_unavailable",
        "keys": [
            {
                "datacenter": "US-KS-2",
                "gpu_name": "NVIDIA L4",
                "cloud_type": "SECURE",
                "gpu_count": 1,
            },
        ],
        "selected_key": None,
    }


def test_stage4_keeps_available_pod_lease_in_attempt_chain() -> None:
    cache = AvailabilityCache()
    cache.set_available("US-KS-2", "NVIDIA L4", "SECURE", 1, available=True)
    request = RoutingRequest(capability_name="vision.yolov11")
    providers = [
        _pod_provider(
            "prov_pod",
            fallback_chain=["prov_public"],
        ),
        _provider(
            "prov_public",
            provider_type=ProviderType.PUBLIC_ENDPOINT,
            priority=2,
            config={"fallback_for": ["prov_pod"]},
        ),
    ]

    plan = plan_route(
        request,
        providers,
        capability=_capability(),
        now=_NOW,
        availability_cache=cache,
    )

    assert plan.fallback_chain == ("prov_pod", "prov_public")
    assert plan.capacity_decisions[0].available is True
    assert plan.capacity_decisions[0].selected_key is not None
    assert plan.eliminated == ()


def test_stage4_probes_only_pod_lease_candidates_after_ranking() -> None:
    class RecordingCache:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, int]] = []

        def is_available(
            self,
            datacenter: str,
            gpu_name: str,
            cloud_type: str,
            gpu_count: int,
        ) -> bool:
            self.calls.append((datacenter, gpu_name, cloud_type, gpu_count))
            return True

    cache = RecordingCache()
    request = RoutingRequest(capability_name="vision.yolov11")
    providers = [
        _provider(
            "prov_queue",
            provider_type=ProviderType.SERVERLESS_QUEUE,
            priority=1,
        ),
        _pod_provider("prov_pod", priority=2),
    ]

    plan = plan_route(
        request,
        providers,
        capability=_capability(),
        now=_NOW,
        availability_cache=cache,
    )

    assert plan.ranked_candidates[0].provider_id == "prov_queue"
    assert cache.calls == [("US-KS-2", "NVIDIA L4", "SECURE", 1)]
    assert [decision.provider_id for decision in plan.capacity_decisions] == [
        "prov_pod",
    ]


def test_stage4_treats_missing_cache_entry_as_capacity_unavailable() -> None:
    request = RoutingRequest(capability_name="vision.yolov11")
    providers = [
        _pod_provider("prov_pod", fallback_chain=["prov_public"]),
        _provider(
            "prov_public",
            provider_type=ProviderType.PUBLIC_ENDPOINT,
            priority=2,
            config={"fallback_for": ["prov_pod"]},
        ),
    ]

    plan = plan_route(
        request,
        providers,
        capability=_capability(),
        now=_NOW,
        availability_cache=AvailabilityCache(),
    )

    assert plan.selected_provider_id == "prov_public"
    assert plan.capacity_decisions[0].available is None
    assert plan.capacity_decisions[0].reason == "capacity_unknown"
    assert plan.dropped_provider_reasons == {
        "prov_pod": [ProviderEliminated.CAPACITY_UNAVAILABLE.value],
    }
