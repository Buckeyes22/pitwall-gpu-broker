"""Property-based tests for counterfactual time-machine reports."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability
from pitwall.finops.time_machine import (
    CounterfactualScenario,
    HistoricalRoutingDecision,
    TimeMachineReplay,
)
from pitwall.routing import PlanningContext, RoutingRequest

pytestmark = pytest.mark.property

_NOW = datetime(2026, 6, 2, 14, 30, tzinfo=UTC)
_CAP_ID = "cap_time_machine_prop"
_CAP_NAME = "embedding.time-machine.prop"

_MONEY = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("10"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


def _context() -> PlanningContext:
    capability = Capability(
        id=_CAP_ID,
        name=_CAP_NAME,
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode="per_request",
        defaults={"execution_timeout_ms": 60_000},
        created_at=_NOW,
        updated_at=_NOW,
    )
    return PlanningContext.replay(
        now=_NOW,
        capability=capability,
        providers=[
            {
                "id": "prov_primary",
                "capability_id": _CAP_ID,
                "name": "prov_primary",
                "provider_type": ProviderType.PUBLIC_ENDPOINT.value,
                "region": "US-KS-2",
                "cost": {"kind": "per_request", "per_request": "0.250000"},
                "config": {"cost": {"kind": "per_request", "per_request": "0.250000"}},
                "priority": 1,
                "enabled": True,
                "health_status": "healthy",
            }
        ],
    )


def _decision(index: int, actual_cost: Decimal) -> HistoricalRoutingDecision:
    return HistoricalRoutingDecision(
        workload_id=f"wkl_{index}",
        request=RoutingRequest(capability_name=_CAP_NAME, capability_id=_CAP_ID),
        payload={},
        actual_provider_id="prov_primary",
        actual_cost_usd=actual_cost,
    )


@given(actual_costs=st.lists(_MONEY, min_size=0, max_size=20))
def test_report_totals_equal_sum_of_workload_deltas(actual_costs: list[Decimal]) -> None:
    report = TimeMachineReplay(_context()).replay(
        scenario=CounterfactualScenario(scenario_id="totals"),
        decisions=[_decision(index, actual_cost) for index, actual_cost in enumerate(actual_costs)],
    )

    line_delta_total = sum(
        (line.cost_delta_usd for line in report.workloads),
        Decimal("0.000000"),
    )
    assert report.summary.cost_delta_usd == report.summary.counterfactual_total_usd - (
        report.summary.actual_total_usd
    )
    assert report.summary.cost_delta_usd == line_delta_total
