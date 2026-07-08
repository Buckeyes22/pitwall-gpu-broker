"""Hermetic unit tests for the reservation / warm-pool recommender."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability
from pitwall.cost.simulator import WhatIfSimulator, WhatIfWorkload
from pitwall.finops.burn_rate import BurnRateForecast
from pitwall.finops.reservations import (
    DemandForecast,
    ReservationCandidate,
    ReservationLine,
    recommend_reservations,
)
from pitwall.routing import PlanningContext, RoutingRequest

_NOW = dt.datetime(2026, 6, 2, 14, 30, tzinfo=dt.UTC)
_CAP_ID = "cap_reserve"
_CAP_NAME = "llm.reserve"


def _capability() -> Capability:
    return Capability(
        id=_CAP_ID,
        name=_CAP_NAME,
        version="1.0.0",
        **{"class": "llm"},
        cost_mode="per_request",
        defaults={"execution_timeout_ms": 60_000},
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    provider_id: str = "prov_primary",
    *,
    per_request: str = "1.00",
    priority: int = 1,
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "id": provider_id,
        "capability_id": _CAP_ID,
        "name": provider_id,
        "provider_type": ProviderType.PUBLIC_ENDPOINT.value,
        "region": "US-KS-2",
        "config": {"cost": {"per_request": per_request}},
        "priority": priority,
        "enabled": enabled,
        "health_status": "healthy",
    }


def _simulator(*providers: dict[str, object]) -> WhatIfSimulator:
    context = PlanningContext.replay(
        now=_NOW,
        providers=providers or (_provider(),),
        capability=_capability(),
    )
    return WhatIfSimulator(context)


def _demand(count: int, *, window_hours: str = "1") -> DemandForecast:
    request = RoutingRequest(capability_name=_CAP_NAME, capability_id=_CAP_ID)
    return DemandForecast(
        name="next-hour",
        workloads=tuple(WhatIfWorkload(request=request, payload={}) for _ in range(count)),
        window_hours=Decimal(window_hours),
    )


def _zero_marginal_plan(
    plan_id: str,
    *,
    provider_id: str = "prov_primary",
    warm_pool_size: int = 0,
    reserved_units: int = 0,
    unit_capacity: int = 1,
    hourly_commitment: str = "0",
    upfront: str = "0",
) -> ReservationCandidate:
    return ReservationCandidate(
        plan_id=plan_id,
        reserves=(
            ReservationLine(
                provider_id=provider_id,
                reserved_units=reserved_units,
                warm_pool_size=warm_pool_size,
                unit_capacity=unit_capacity,
                hourly_commitment_usd=Decimal(hourly_commitment),
                upfront_usd=Decimal(upfront),
            ),
        ),
        price_overrides={provider_id: {"kind": "per_request", "per_request": "0"}},
    )


def test_recommends_on_demand_when_reservation_costs_more_than_baseline() -> None:
    recommendation = recommend_reservations(
        demand=_demand(2),
        simulator=_simulator(),
        candidates=[
            _zero_marginal_plan(
                "warm_three",
                warm_pool_size=3,
                hourly_commitment="1.00",
            ),
        ],
    )

    assert recommendation.action == "on_demand"
    assert recommendation.baseline.total_cost_usd == Decimal("2.000000")
    assert recommendation.recommended.plan_id == "on_demand"
    assert recommendation.projected_savings_usd == Decimal("0.000000")


def test_recommends_warm_pool_when_fixed_commitment_saves_money() -> None:
    recommendation = recommend_reservations(
        demand=_demand(3),
        simulator=_simulator(),
        candidates=[
            _zero_marginal_plan(
                "warm_three",
                warm_pool_size=3,
                hourly_commitment="0.25",
            ),
        ],
    )

    assert recommendation.action == "reserve"
    assert recommendation.recommended.plan_id == "warm_three"
    assert recommendation.recommended.covered_workloads == 3
    assert recommendation.recommended.total_cost_usd == Decimal("0.750000")
    assert recommendation.projected_savings_usd == Decimal("2.250000")
    assert recommendation.recommended.plan.reserves[0].warm_pool_size == 3


def test_capacity_limited_reservation_overflows_remaining_demand_to_on_demand() -> None:
    recommendation = recommend_reservations(
        demand=_demand(3),
        simulator=_simulator(),
        candidates=[
            _zero_marginal_plan(
                "warm_two",
                warm_pool_size=2,
                hourly_commitment="0.25",
            ),
        ],
    )

    assert recommendation.recommended.plan_id == "warm_two"
    assert recommendation.recommended.fixed_cost_usd == Decimal("0.500000")
    assert recommendation.recommended.marginal_cost_usd == Decimal("1.000000")
    assert recommendation.recommended.covered_workloads == 2
    assert recommendation.recommended.on_demand_overflow_workloads == 1
    assert recommendation.recommended.total_cost_usd == Decimal("1.500000")


def test_blocked_when_neither_reservation_nor_on_demand_can_route_demand() -> None:
    recommendation = recommend_reservations(
        demand=_demand(1),
        simulator=_simulator(_provider(enabled=False)),
        candidates=[
            _zero_marginal_plan(
                "warm_one",
                warm_pool_size=1,
                hourly_commitment="0.25",
            ),
        ],
    )

    assert recommendation.action == "blocked"
    assert recommendation.baseline.meets_demand is False
    assert recommendation.recommended.meets_demand is False
    assert recommendation.recommended.unmet_workloads == 1


def test_burn_rate_metadata_projects_remaining_budget_after_recommended_plan() -> None:
    burn_rate = BurnRateForecast(
        burn_rate_usd_per_day=Decimal("2.000000"),
        projected_exhaustion=_NOW + dt.timedelta(days=5),
        trend="stable",
        confidence=Decimal("1.000000"),
        budget_usd=Decimal("20.000000"),
        remaining_budget_usd=Decimal("10.000000"),
        runway_days=Decimal("5.000000"),
    )

    recommendation = recommend_reservations(
        demand=_demand(3),
        simulator=_simulator(),
        candidates=[
            _zero_marginal_plan(
                "warm_three",
                warm_pool_size=1,
                unit_capacity=3,
                hourly_commitment="2.00",
            ),
        ],
        burn_rate_forecast=burn_rate,
    )

    assert recommendation.recommended.total_cost_usd == Decimal("2.000000")
    assert recommendation.recommended.budget_after_plan_usd == Decimal("8.000000")
    assert recommendation.recommended.runway_days_after_plan == Decimal("4.000000")


def test_to_dict_is_deterministic_and_json_serializable() -> None:
    args = {
        "demand": _demand(2),
        "simulator": _simulator(),
        "candidates": [
            _zero_marginal_plan("warm_two", warm_pool_size=2, hourly_commitment="0.25"),
        ],
    }

    first = recommend_reservations(**args)
    second = recommend_reservations(**args)
    first_json = json.dumps(first.to_dict(), sort_keys=True)
    second_json = json.dumps(second.to_dict(), sort_keys=True)

    assert first_json == second_json
    assert json.loads(first_json)["recommended"]["total_cost_usd"] == "0.500000"
    assert json.loads(first_json)["demand"]["workload_count"] == 2


def test_equal_cost_reservation_candidates_use_stable_plan_id_tiebreaker() -> None:
    recommendation = recommend_reservations(
        demand=_demand(2),
        simulator=_simulator(),
        candidates=[
            _zero_marginal_plan("reserve_b", reserved_units=2, hourly_commitment="0.25"),
            _zero_marginal_plan("reserve_a", reserved_units=2, hourly_commitment="0.25"),
        ],
    )

    assert recommendation.action == "reserve"
    assert recommendation.recommended.plan_id == "reserve_a"


def test_candidate_price_overrides_must_have_matching_reserved_capacity() -> None:
    with pytest.raises(ValueError, match="price_overrides must reference reserved providers"):
        ReservationCandidate(
            plan_id="bad",
            reserves=(
                ReservationLine(
                    provider_id="prov_primary",
                    warm_pool_size=1,
                    hourly_commitment_usd=Decimal("1"),
                ),
            ),
            price_overrides={"prov_other": {"kind": "per_request", "per_request": "0"}},
        )
