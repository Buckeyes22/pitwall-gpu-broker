"""Property coverage for Autopilot safety invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.autopilot import (
    ActionApplyResult,
    AutopilotActionKind,
    AutopilotController,
    AutopilotSignal,
)
from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability
from pitwall.cost import WhatIfSimulator, WhatIfWorkload
from pitwall.policy import PolicySet
from pitwall.routing import PlanningContext, RoutingRequest

_NOW = datetime(2026, 6, 2, 14, 0, tzinfo=UTC)
_CAPABILITY_ID = "cap_autopilot_property"
_CAPABILITY_NAME = "embedding.autopilot-property"


class RecordingExecutor:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def apply(self, action: Any) -> ActionApplyResult:
        self.applied.append(action.action_id)
        return ActionApplyResult(
            action_id=action.action_id,
            applied=True,
            message="recorded",
        )


def _capability() -> Capability:
    return Capability(
        id=_CAPABILITY_ID,
        name=_CAPABILITY_NAME,
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _simulator() -> WhatIfSimulator:
    context = PlanningContext.replay(
        now=_NOW,
        providers=[
            {
                "id": "prov_property",
                "capability_id": _CAPABILITY_ID,
                "name": "prov_property",
                "provider_type": ProviderType.SERVERLESS_QUEUE.value,
                "priority": 1,
                "enabled": True,
                "health_status": "healthy",
                "cold_start_p50_ms": 0,
                "recent_error_rate": 0.0,
                "config": {"cost": {"per_second_active": "0.001"}},
            }
        ],
        capability=_capability(),
    )
    return WhatIfSimulator(
        context,
        budget_usd=Decimal("10.000000"),
        current_spend_usd=Decimal("0.000000"),
    )


def _workload() -> WhatIfWorkload:
    return WhatIfWorkload(
        request=RoutingRequest(
            capability_name=_CAPABILITY_NAME,
            capability_id=_CAPABILITY_ID,
        )
    )


def _signal(index: int, priority: int) -> AutopilotSignal:
    return AutopilotSignal(
        signal_id=f"sig-{index}",
        source="property",
        action_kind=AutopilotActionKind.SET_WARM_CAPACITY,
        target_kind="provider",
        target_id="prov_property",
        reason="property-generated safe signal",
        priority=priority,
        confidence=Decimal("0.90"),
        params={"target_count": index + 1},
        policy_provider={
            "id": "prov_property",
            "provider_type": ProviderType.SERVERLESS_QUEUE.value,
            "config": {},
        },
        simulation_workloads=(_workload(),),
    )


@pytest.mark.property
@given(st.lists(st.integers(min_value=1, max_value=100), min_size=0, max_size=5))
def test_shadow_mode_never_applies_actions(priorities: list[int]) -> None:
    executor = RecordingExecutor()
    controller = AutopilotController(
        policy_set=PolicySet(),
        simulator=_simulator(),
        executor=executor,
    )
    signals = [_signal(index, priority) for index, priority in enumerate(priorities)]

    result = controller.run(now=_NOW, signals=signals)

    assert executor.applied == []
    assert result.applied_count == 0
    assert all(decision.outcome == "shadowed" for decision in result.decisions)
