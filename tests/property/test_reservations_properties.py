"""Property-based tests for the reservation / warm-pool recommender."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability
from pitwall.cost.simulator import WhatIfSimulator, WhatIfWorkload
from pitwall.finops.reservations import (
    DemandForecast,
    ReservationCandidate,
    ReservationLine,
    recommend_reservations,
)
from pitwall.routing import PlanningContext, RoutingRequest

pytestmark = pytest.mark.property

_NOW = dt.datetime(2026, 6, 2, 14, 30, tzinfo=dt.UTC)
_CAP_ID = "cap_reserve_prop"
_CAP_NAME = "llm.reserve.prop"


fixed_costs = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("2"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
).map(lambda value: Decimal(str(value)))


def _simulator() -> WhatIfSimulator:
    capability = Capability(
        id=_CAP_ID,
        name=_CAP_NAME,
        version="1.0.0",
        **{"class": "llm"},
        cost_mode="per_request",
        defaults={"execution_timeout_ms": 60_000},
        created_at=_NOW,
        updated_at=_NOW,
    )
    context = PlanningContext.replay(
        now=_NOW,
        capability=capability,
        providers=[
            {
                "id": "prov_primary",
                "capability_id": _CAP_ID,
                "name": "prov_primary",
                "provider_type": ProviderType.PUBLIC_ENDPOINT.value,
                "region": "US-KS-2",
                "config": {"cost": {"per_request": "1.00"}},
                "priority": 1,
                "enabled": True,
                "health_status": "healthy",
            }
        ],
    )
    return WhatIfSimulator(context)


def _demand() -> DemandForecast:
    request = RoutingRequest(capability_name=_CAP_NAME, capability_id=_CAP_ID)
    return DemandForecast(
        name="single-request",
        workloads=(WhatIfWorkload(request=request, payload={}),),
        window_hours=Decimal("1"),
    )


def _candidate(plan_id: str, fixed_cost_usd: Decimal) -> ReservationCandidate:
    return ReservationCandidate(
        plan_id=plan_id,
        reserves=(
            ReservationLine(
                provider_id="prov_primary",
                reserved_units=1,
                unit_capacity=1,
                upfront_usd=fixed_cost_usd,
            ),
        ),
        price_overrides={"prov_primary": {"kind": "per_request", "per_request": "0"}},
    )


@given(first=fixed_costs, second=fixed_costs, third=fixed_costs)
def test_recommended_plan_cost_is_minimum_eligible_total(
    first: Decimal,
    second: Decimal,
    third: Decimal,
) -> None:
    recommendation = recommend_reservations(
        demand=_demand(),
        simulator=_simulator(),
        candidates=[
            _candidate("reserve_a", first),
            _candidate("reserve_b", second),
            _candidate("reserve_c", third),
        ],
    )

    eligible_totals = [
        evaluation.total_cost_usd
        for evaluation in (recommendation.baseline, *recommendation.evaluations)
        if evaluation.meets_demand
    ]
    assert recommendation.recommended.total_cost_usd == min(eligible_totals)
