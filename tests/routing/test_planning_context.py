"""Replay-context coverage for deterministic route planning."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability
from pitwall.routing import PlanningContext, ProviderEliminated, RoutingRequest, plan_route
from pitwall.runpod_client.availability import (
    get_global_availability_cache,
    reset_global_availability_cache,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_HISTORICAL_NOW = datetime(2025, 12, 15, 8, 30, 0, tzinfo=UTC)


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


def _pod_provider(
    provider_id: str = "prov_pod",
    *,
    priority: int = 1,
    datacenter: str = "US-KS-2",
    gpu_name: str = "NVIDIA L4",
    gpu_count: int = 1,
) -> dict[str, object]:
    return {
        "id": provider_id,
        "capability_id": "cap_vision",
        "name": provider_id,
        "provider_type": ProviderType.POD_LEASE.value,
        "region": datacenter,
        "cloud_type": "SECURE",
        "config": {
            "gpu_type_priority": [gpu_name],
            "gpu_count": gpu_count,
            "fallback_chain": ["prov_public"],
        },
        "priority": priority,
        "health_status": "healthy",
        "enabled": True,
    }


def _public_provider(priority: int = 2) -> dict[str, object]:
    return {
        "id": "prov_public",
        "capability_id": "cap_vision",
        "name": "prov_public",
        "provider_type": ProviderType.PUBLIC_ENDPOINT.value,
        "region": "US-KS-2",
        "config": {"fallback_for": ["prov_pod"]},
        "priority": priority,
        "health_status": "healthy",
        "enabled": True,
    }


def _plan_bytes(plan: object) -> bytes:
    return json.dumps(plan.to_dict(), sort_keys=True).encode()


def test_planning_context_public_import_succeeds_in_fresh_process() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pitwall.routing import PlanningContext; "
            "assert PlanningContext.__name__ == 'PlanningContext'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_replay_context_yields_byte_identical_plans_from_frozen_inputs() -> None:
    request = RoutingRequest(capability_name="vision.yolov11")
    pod = _pod_provider()
    public = _public_provider()
    context = PlanningContext.replay(
        now=_NOW,
        providers=[pod, public],
        capability=_capability(),
        availability_entries=[("US-KS-2", "NVIDIA L4", "SECURE", 1, False)],
    )

    reset_global_availability_cache()
    get_global_availability_cache().set_available(
        "US-KS-2",
        "NVIDIA L4",
        "SECURE",
        1,
        available=True,
    )
    pod["provider_type"] = ProviderType.PUBLIC_ENDPOINT.value

    first = plan_route(request, context=context)
    second = plan_route(request, context=context)

    assert _plan_bytes(first) == _plan_bytes(second)
    assert first.selected_provider_id == "prov_public"
    assert first.dropped_provider_reasons == {
        "prov_pod": [ProviderEliminated.CAPACITY_UNAVAILABLE.value],
    }


def test_replay_context_uses_historical_capacity_snapshot() -> None:
    request = RoutingRequest(
        capability_name="vision.yolov11",
        required_gpu_class="NVIDIA A100",
    )
    context = PlanningContext.replay(
        now=_HISTORICAL_NOW,
        providers=[
            _pod_provider(
                datacenter="US-CA-1",
                gpu_name="NVIDIA A100",
                gpu_count=2,
            ),
            _public_provider(),
        ],
        capability=_capability(),
        availability_entries=[("US-CA-1", "NVIDIA A100", "SECURE", 2, True)],
    )
    reset_global_availability_cache()
    get_global_availability_cache().set_available(
        "US-CA-1",
        "NVIDIA A100",
        "SECURE",
        2,
        available=False,
    )

    plan = plan_route(request, context=context)

    assert plan.selected_provider_id == "prov_pod"
    assert plan.capacity_decisions[0].selected_key is not None
    assert plan.capacity_decisions[0].selected_key.to_dict() == {
        "datacenter": "US-CA-1",
        "gpu_name": "NVIDIA A100",
        "cloud_type": "SECURE",
        "gpu_count": 2,
    }


def test_live_planning_without_context_uses_current_global_availability() -> None:
    request = RoutingRequest(capability_name="vision.yolov11")
    providers = [_pod_provider(), _public_provider()]
    reset_global_availability_cache()
    cache = get_global_availability_cache()
    cache.set_available("US-KS-2", "NVIDIA L4", "SECURE", 1, available=False)

    unavailable_plan = plan_route(
        request,
        providers,
        capability=_capability(),
        now=_NOW,
    )
    cache.set_available("US-KS-2", "NVIDIA L4", "SECURE", 1, available=True)
    available_plan = plan_route(
        request,
        providers,
        capability=_capability(),
        now=_NOW,
    )

    assert unavailable_plan.selected_provider_id == "prov_public"
    assert available_plan.selected_provider_id == "prov_pod"
