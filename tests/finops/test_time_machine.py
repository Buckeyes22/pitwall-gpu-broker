from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability
from pitwall.finops import TimeMachineReplay
from pitwall.finops.time_machine import (
    CounterfactualScenario,
    HistoricalRoutingDecision,
)
from pitwall.routing import Hints, PlanningContext, RoutingRequest

_NOW = datetime(2026, 6, 2, 14, 30, tzinfo=UTC)
_CAP_ID = "cap_time_machine"
_CAP_NAME = "embedding.time-machine"


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


def _cost(rate: str) -> dict[str, object]:
    return {"per_second_active": rate}


def _provider(
    provider_id: str,
    *,
    rate: str = "0.001",
    provider_type: ProviderType = ProviderType.SERVERLESS_QUEUE,
    priority: int = 1,
    config: dict[str, object] | None = None,
    region: str = "US-KS-2",
    cloud_type: str | None = None,
    enabled: bool = True,
) -> dict[str, object]:
    cost = _cost(rate)
    merged_config: dict[str, object] = {"cost": cost}
    if config is not None:
        merged_config.update(config)
    return {
        "id": provider_id,
        "capability_id": _CAP_ID,
        "name": provider_id,
        "provider_type": provider_type.value,
        "region": region,
        "cloud_type": cloud_type,
        "cost": cost,
        "cost_per_second_active": rate,
        "per_second_active": rate,
        "config": merged_config,
        "priority": priority,
        "enabled": enabled,
        "health_status": "healthy",
        "cold_start_p50_ms": 0,
        "recent_error_rate": 0.0,
    }


def _public_provider(provider_id: str, *, fee: str, priority: int = 2) -> dict[str, object]:
    cost: dict[str, object] = {"kind": "per_request", "per_request": fee}
    return {
        "id": provider_id,
        "capability_id": _CAP_ID,
        "name": provider_id,
        "provider_type": ProviderType.PUBLIC_ENDPOINT.value,
        "region": "US-KS-2",
        "cost": cost,
        "config": {"cost": cost},
        "priority": priority,
        "enabled": True,
        "health_status": "healthy",
        "cold_start_p50_ms": 0,
        "recent_error_rate": 0.0,
    }


def _context(*providers: dict[str, object]) -> PlanningContext:
    return PlanningContext.replay(
        now=_NOW,
        providers=providers,
        capability=_capability(),
    )


def _request(*, cost_sensitive: bool = False) -> RoutingRequest:
    return RoutingRequest(
        capability_name=_CAP_NAME,
        capability_id=_CAP_ID,
        hints=Hints(cost_sensitive=cost_sensitive),
    )


def _decision(
    workload_id: str = "wkl_actual",
    *,
    actual_provider_id: str | None = "prov_fast",
    actual_cost_usd: Decimal = Decimal("0.060000"),
    request: RoutingRequest | None = None,
) -> HistoricalRoutingDecision:
    return HistoricalRoutingDecision(
        workload_id=workload_id,
        request=request or _request(cost_sensitive=True),
        actual_provider_id=actual_provider_id,
        actual_cost_usd=actual_cost_usd,
        payload={},
    )


def test_price_counterfactual_reports_route_and_cost_delta_against_actual() -> None:
    context = _context(
        _provider("prov_fast", rate="0.001"),
        _provider("prov_cheap", rate="0.002", priority=2),
    )
    scenario = CounterfactualScenario(
        scenario_id="raise-fast-price",
        price_overrides={"prov_fast": _cost("0.020")},
    )

    report = TimeMachineReplay(context).replay(
        scenario=scenario,
        decisions=[_decision()],
    )

    line = report.workloads[0]
    assert line.actual_provider_id == "prov_fast"
    assert line.counterfactual_provider_id == "prov_cheap"
    assert line.routed_differently is True
    assert line.actual_cost_usd == Decimal("0.060000")
    assert line.counterfactual_cost_usd == Decimal("0.120000")
    assert line.cost_delta_usd == Decimal("0.060000")
    assert report.summary.cost_delta_usd == Decimal("0.060000")


def test_delta_uses_recorded_actual_cost_not_original_estimate() -> None:
    context = _context(
        _provider("prov_fast", rate="0.001"),
        _provider("prov_cheap", rate="0.002", priority=2),
    )
    scenario = CounterfactualScenario(
        scenario_id="actual-cost-source",
        price_overrides={"prov_fast": _cost("0.020")},
    )

    report = TimeMachineReplay(context).replay(
        scenario=scenario,
        decisions=[_decision(actual_cost_usd=Decimal("0.070000"))],
    )

    assert report.workloads[0].counterfactual_cost_usd == Decimal("0.120000")
    assert report.workloads[0].cost_delta_usd == Decimal("0.050000")
    assert report.summary.actual_total_usd == Decimal("0.070000")


def test_provider_override_can_disable_historical_provider() -> None:
    context = _context(
        _provider(
            "prov_primary",
            rate="0.001",
            config={"fallback_chain": ["prov_backup"]},
        ),
        _provider("prov_backup", rate="0.003", priority=2),
    )
    scenario = CounterfactualScenario(
        scenario_id="disable-primary",
        provider_overrides={"prov_primary": {"enabled": False}},
    )

    report = TimeMachineReplay(context).replay(
        scenario=scenario,
        decisions=[_decision(actual_provider_id="prov_primary")],
    )

    line = report.workloads[0]
    assert line.counterfactual_provider_id == "prov_backup"
    assert line.counterfactual_fallback_chain == ("prov_backup",)
    assert line.cost_delta_usd == Decimal("0.120000")


def test_capacity_override_replays_stage4_without_live_cache() -> None:
    context = _context(
        _provider(
            "prov_pod",
            rate="0.001",
            provider_type=ProviderType.POD_LEASE,
            cloud_type="SECURE",
            config={
                "gpu_type_priority": ["NVIDIA L4"],
                "gpu_count": 1,
                "fallback_chain": ["prov_public"],
            },
        ),
        _public_provider("prov_public", fee="0.004"),
    )
    scenario = CounterfactualScenario(
        scenario_id="pod-capacity-miss",
        availability_entries=[("US-KS-2", "NVIDIA L4", "SECURE", 1, False)],
    )

    report = TimeMachineReplay(context).replay(
        scenario=scenario,
        decisions=[
            _decision(
                actual_provider_id="prov_pod",
                actual_cost_usd=Decimal("0.060000"),
            )
        ],
    )

    line = report.workloads[0]
    assert line.counterfactual_provider_id == "prov_public"
    assert line.counterfactual_cost_usd == Decimal("0.004000")
    assert line.cost_delta_usd == Decimal("-0.056000")
    assert line.projection.plan.capacity_decisions[0].reason == "capacity_unavailable"


def test_batch_summary_totals_counts_and_provider_counts_are_deterministic() -> None:
    context = _context(
        _provider("prov_fast", rate="0.001"),
        _provider("prov_cheap", rate="0.002", priority=2),
    )
    scenario = CounterfactualScenario(
        scenario_id="batch",
        price_overrides={"prov_fast": _cost("0.020")},
    )

    report = TimeMachineReplay(context).replay(
        scenario=scenario,
        decisions=[
            _decision("wkl_1", actual_provider_id="prov_fast"),
            _decision("wkl_2", actual_provider_id="prov_fast"),
        ],
    )

    assert report.summary.workload_count == 2
    assert report.summary.changed_route_count == 2
    assert report.summary.unroutable_count == 0
    assert report.summary.actual_total_usd == Decimal("0.120000")
    assert report.summary.counterfactual_total_usd == Decimal("0.240000")
    assert report.summary.cost_delta_usd == Decimal("0.120000")
    assert dict(report.summary.actual_provider_counts) == {"prov_fast": 2}
    assert dict(report.summary.counterfactual_provider_counts) == {"prov_cheap": 2}


def test_to_dict_is_json_safe_and_stable() -> None:
    context = _context(
        _provider("prov_fast", rate="0.001"),
        _provider("prov_cheap", rate="0.002", priority=2),
    )
    scenario = CounterfactualScenario(
        scenario_id="stable-json",
        description="Replay a deterministic price curve.",
        price_overrides={"prov_fast": _cost("0.020")},
    )
    replay = TimeMachineReplay(context)

    first = replay.replay(scenario=scenario, decisions=[_decision()]).to_dict()
    second = replay.replay(scenario=scenario, decisions=[_decision()]).to_dict()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["summary"]["cost_delta_usd"] == "0.060000"
    assert first["scenario"]["id"] == "stable-json"
    assert first["workloads"][0]["counterfactual"]["cost_usd"] == "0.120000"


def test_scenario_inputs_are_frozen_after_construction() -> None:
    context = _context(
        _provider("prov_primary", rate="0.001"),
        _provider("prov_backup", rate="0.003", priority=2),
    )
    provider_patch: dict[str, object] = {"enabled": False}
    scenario = CounterfactualScenario(
        scenario_id="frozen-overrides",
        provider_overrides={"prov_primary": provider_patch},
    )
    provider_patch["enabled"] = True

    report = TimeMachineReplay(context).replay(
        scenario=scenario,
        decisions=[_decision(actual_provider_id="prov_primary")],
    )

    assert report.workloads[0].counterfactual_provider_id == "prov_backup"


def test_unroutable_counterfactual_reports_zero_cost_and_negative_delta() -> None:
    context = _context(_provider("prov_primary", rate="0.001"))
    scenario = CounterfactualScenario(
        scenario_id="remove-provider",
        removed_provider_ids=["prov_primary"],
    )

    report = TimeMachineReplay(context).replay(
        scenario=scenario,
        decisions=[
            _decision(
                actual_provider_id="prov_primary",
                actual_cost_usd=Decimal("0.060000"),
            )
        ],
    )

    line = report.workloads[0]
    assert line.counterfactual_provider_id is None
    assert line.counterfactual_cost_usd == Decimal("0.000000")
    assert line.cost_delta_usd == Decimal("-0.060000")
    assert report.summary.unroutable_count == 1
    assert report.summary.changed_route_count == 1
