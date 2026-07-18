from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability
from pitwall.cost.simulator import WhatIfSimulator, WhatIfWorkload
from pitwall.routing import Hints, PlanningContext, RoutingRequest

_NOW = datetime(2026, 6, 2, 14, 30, tzinfo=UTC)
_CAP_ID = "cap_whatif"
_CAP_NAME = "embedding.what-if"


def _capability(cost_mode: str = "per_second", timeout_ms: int = 60_000) -> Capability:
    return Capability(
        id=_CAP_ID,
        name=_CAP_NAME,
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode=cost_mode,
        defaults={"execution_timeout_ms": timeout_ms},
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    provider_id: str,
    *,
    provider_type: ProviderType = ProviderType.SERVERLESS_QUEUE,
    priority: int = 1,
    cost: dict[str, object] | None = None,
    config: dict[str, object] | None = None,
    region: str = "US-KS-2",
    cloud_type: str | None = None,
    enabled: bool = True,
) -> dict[str, object]:
    merged_config: dict[str, object] = {}
    if cost is not None:
        merged_config["cost"] = cost
    if config is not None:
        merged_config.update(config)
    return {
        "id": provider_id,
        "capability_id": _CAP_ID,
        "name": provider_id,
        "provider_type": provider_type.value,
        "region": region,
        "cloud_type": cloud_type,
        "config": merged_config,
        "priority": priority,
        "enabled": enabled,
        "health_status": "healthy",
        "cold_start_p50_ms": 0,
        "recent_error_rate": 0.0,
    }


def _context(
    *providers: dict[str, object], capability: Capability | None = None
) -> PlanningContext:
    return PlanningContext.replay(
        now=_NOW,
        providers=providers,
        capability=capability or _capability(),
    )


def _request() -> RoutingRequest:
    return RoutingRequest(capability_name=_CAP_NAME, capability_id=_CAP_ID)


def _cost(rate: str) -> dict[str, object]:
    return {"per_second_active": rate}


def test_price_override_replays_planner_against_hypothetical_prices() -> None:
    context = _context(
        _provider("prov_fast", cost=_cost("0.001")),
        _provider("prov_cheap", priority=2, cost=_cost("0.002")),
    )
    request = RoutingRequest(
        capability_name=_CAP_NAME,
        capability_id=_CAP_ID,
        hints=Hints(cost_sensitive=True),
    )

    projection = WhatIfSimulator(
        context,
        price_overrides={"prov_fast": _cost("0.020")},
    ).simulate(request)

    assert projection.plan.selected_provider_id == "prov_cheap"
    assert projection.selected_cost is not None
    assert projection.selected_cost.provider_id == "prov_cheap"
    assert projection.selected_cost.upper_bound_usd == Decimal("0.120000")


def test_budget_headroom_uses_selected_provider_upper_bound() -> None:
    context = _context(_provider("prov_primary", cost=_cost("0.002")))

    projection = WhatIfSimulator(
        context,
        budget_usd=Decimal("1.000000"),
        current_spend_usd=Decimal("0.900000"),
    ).simulate(_request())

    assert projection.reserved_usd == Decimal("0.120000")
    assert projection.projected_spend_usd == Decimal("1.020000")
    assert projection.budget_headroom_usd == Decimal("-0.020000")
    assert projection.would_exceed_budget is True


def test_cost_breakdown_reports_each_planned_attempt() -> None:
    context = _context(
        _provider(
            "prov_primary",
            cost=_cost("0.001"),
            config={"fallback_chain": ["prov_public"]},
        ),
        _provider(
            "prov_public",
            provider_type=ProviderType.PUBLIC_ENDPOINT,
            priority=2,
            cost={"per_request": "0.005"},
            config={"fallback_for": ["prov_primary"]},
        ),
        capability=_capability(cost_mode="per_second"),
    )
    simulator = WhatIfSimulator(
        context,
        price_overrides={"prov_public": {"kind": "per_request", "per_request": "0.005"}},
    )

    projection = simulator.simulate(_request())

    assert projection.plan.fallback_chain == ("prov_primary", "prov_public")
    assert [(cost.attempt, cost.provider_id) for cost in projection.attempt_costs] == [
        (1, "prov_primary"),
        (2, "prov_public"),
    ]
    assert [cost.pricing_kind for cost in projection.attempt_costs] == [
        "gpu_hour",
        "per_request",
    ]
    assert [cost.upper_bound_usd for cost in projection.attempt_costs] == [
        Decimal("0.060000"),
        Decimal("0.005000"),
    ]
    assert projection.reserved_usd == Decimal("0.060000")


def test_projection_with_no_viable_provider_has_zero_reserved_cost() -> None:
    context = _context(
        _provider("prov_disabled", cost=_cost("0.002"), enabled=False),
    )

    projection = WhatIfSimulator(
        context,
        budget_usd=Decimal("1.000000"),
        current_spend_usd=Decimal("0.250000"),
    ).simulate(_request())

    assert projection.plan.selected_provider_id is None
    assert projection.attempt_costs == ()
    assert projection.selected_cost is None
    assert projection.reserved_usd == Decimal("0.000000")
    assert projection.budget_headroom_usd == Decimal("0.750000")
    assert projection.would_exceed_budget is False


def test_missing_selected_provider_cost_raises_value_error() -> None:
    context = _context(_provider("prov_missing_cost"))

    with pytest.raises(ValueError, match="per_second_active"):
        WhatIfSimulator(context).simulate(_request())


def test_projection_to_dict_is_deterministic_and_json_serializable() -> None:
    context = _context(
        _provider("prov_primary", cost=_cost("0.001")),
        _provider("prov_secondary", priority=2, cost=_cost("0.002")),
    )
    simulator = WhatIfSimulator(context, budget_usd="2.00", current_spend_usd="0.25")

    first = simulator.simulate(_request())
    second = simulator.simulate(_request())

    first_json = json.dumps(first.to_dict(), sort_keys=True)
    second_json = json.dumps(second.to_dict(), sort_keys=True)
    assert first_json == second_json
    assert json.loads(first_json)["cost"]["reserved_usd"] == "0.060000"


def test_simulate_workloads_accumulates_candidate_workload_costs() -> None:
    context = _context(_provider("prov_primary", cost=_cost("0.001")))
    simulator = WhatIfSimulator(
        context,
        budget_usd=Decimal("0.200000"),
        current_spend_usd=Decimal("0.050000"),
    )

    batch = simulator.simulate_workloads(
        [
            WhatIfWorkload(request=_request(), payload={}),
            WhatIfWorkload(request=_request(), payload={}),
        ]
    )

    assert [projection.current_spend_usd for projection in batch.projections] == [
        Decimal("0.050000"),
        Decimal("0.110000"),
    ]
    assert batch.total_reserved_usd == Decimal("0.120000")
    assert batch.projected_spend_usd == Decimal("0.170000")
    assert batch.budget_headroom_usd == Decimal("0.030000")
    assert batch.would_exceed_budget is False


def test_from_inputs_builds_replay_context_with_capacity_snapshot() -> None:
    pod_provider = _provider(
        "prov_pod",
        provider_type=ProviderType.POD_LEASE,
        cost=_cost("0.001"),
        config={
            "gpu_type_priority": ["NVIDIA L4"],
            "gpu_count": 1,
            "fallback_chain": ["prov_public"],
        },
        cloud_type="SECURE",
    )
    public_provider = _provider(
        "prov_public",
        provider_type=ProviderType.PUBLIC_ENDPOINT,
        priority=2,
        cost={"per_request": "0.004"},
        config={"fallback_for": ["prov_pod"]},
    )

    simulator = WhatIfSimulator.from_inputs(
        now=_NOW,
        providers=[pod_provider, public_provider],
        capability=_capability(),
        availability_entries=[("US-KS-2", "NVIDIA L4", "SECURE", 1, False)],
        price_overrides={"prov_public": {"kind": "per_request", "per_request": "0.004"}},
    )

    projection = simulator.simulate(_request())

    assert projection.plan.selected_provider_id == "prov_public"
    assert projection.plan.capacity_decisions[0].reason == "capacity_unavailable"
    assert projection.selected_cost is not None
    assert projection.selected_cost.upper_bound_usd == Decimal("0.004000")
