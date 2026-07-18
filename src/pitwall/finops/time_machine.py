"""Counterfactual replay reports for historical routing decisions."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any, cast

from pitwall.cost.simulator import (
    AvailabilityEntry,
    PriceOverrides,
    WhatIfProjection,
    WhatIfSimulator,
)
from pitwall.routing.context import (
    AvailabilitySnapshot,
    PlanningContext,
    ProviderInput,
    ProviderSnapshot,
    freeze_provider_snapshot,
)
from pitwall.routing.planner import DEFAULT_BACKOFF_BASE_S, DEFAULT_MAX_ATTEMPTS
from pitwall.routing.types import Hints, ObservedMetrics, RoutePlan, RoutingRequest

_USD_QUANTUM = Decimal("0.000001")
_ZERO_USD = Decimal("0.000000")

type ObservedByProvider = Mapping[str, ObservedMetrics | Mapping[str, object] | float | int]


@dataclass(frozen=True, slots=True)
class HistoricalRoutingDecision:
    """One historical workload decision to replay against counterfactual inputs."""

    workload_id: str
    request: RoutingRequest
    actual_provider_id: str | None
    actual_cost_usd: Decimal
    payload: Mapping[str, Any] = field(default_factory=dict)
    actual_fallback_chain: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workload_id",
            _non_empty_string(self.workload_id, "workload_id"),
        )
        if not isinstance(self.request, RoutingRequest):
            raise TypeError("request must be RoutingRequest")
        object.__setattr__(
            self,
            "actual_provider_id",
            _optional_non_empty_string(self.actual_provider_id, "actual_provider_id"),
        )
        object.__setattr__(
            self,
            "actual_cost_usd",
            _usd(self.actual_cost_usd, "actual_cost_usd"),
        )
        object.__setattr__(
            self,
            "payload",
            _freeze_mapping(cast(Mapping[str, object], self.payload)),
        )
        object.__setattr__(
            self,
            "actual_fallback_chain",
            _fallback_chain(
                self.actual_fallback_chain,
                actual_provider_id=self.actual_provider_id,
            ),
        )

    def to_dict(self) -> dict[str, str | list[str] | None]:
        return {
            "workload_id": self.workload_id,
            "actual_provider_id": self.actual_provider_id,
            "actual_fallback_chain": list(self.actual_fallback_chain),
            "actual_cost_usd": _decimal_to_str(self.actual_cost_usd),
        }


@dataclass(frozen=True, slots=True)
class CounterfactualScenario:
    """Named override set applied to a historical planning context."""

    scenario_id: str
    description: str = ""
    price_overrides: PriceOverrides = field(default_factory=dict)
    provider_overrides: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    removed_provider_ids: Sequence[str] = field(default_factory=tuple)
    additional_providers: Sequence[ProviderInput] = field(default_factory=tuple)
    availability_snapshot: AvailabilitySnapshot | None = None
    availability_entries: Sequence[AvailabilityEntry] = field(default_factory=tuple)
    hints: Hints | None = None
    observed: ObservedMetrics | ObservedByProvider | None = None
    budget_usd: Decimal | None = None
    current_spend_usd: Decimal | None = None
    max_attempts: int | None = None
    backoff_base_s: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scenario_id",
            _non_empty_string(self.scenario_id, "scenario_id"),
        )
        if not isinstance(self.description, str):
            raise TypeError("description must be str")
        object.__setattr__(
            self,
            "price_overrides",
            _freeze_mapping(self.price_overrides),
        )
        object.__setattr__(
            self,
            "provider_overrides",
            _freeze_provider_overrides(self.provider_overrides),
        )
        object.__setattr__(
            self,
            "removed_provider_ids",
            _string_tuple(self.removed_provider_ids, "removed_provider_ids"),
        )
        object.__setattr__(
            self,
            "additional_providers",
            freeze_provider_snapshot(self.additional_providers),
        )
        entries = tuple(self.availability_entries)
        if self.availability_snapshot is not None and entries:
            raise ValueError(
                "availability_snapshot and availability_entries are mutually exclusive"
            )
        object.__setattr__(self, "availability_entries", entries)
        if self.observed is not None and isinstance(self.observed, Mapping):
            object.__setattr__(
                self,
                "observed",
                _freeze_mapping(cast(Mapping[str, object], self.observed)),
            )
        object.__setattr__(self, "budget_usd", _optional_usd(self.budget_usd, "budget_usd"))
        object.__setattr__(
            self,
            "current_spend_usd",
            _optional_usd(self.current_spend_usd, "current_spend_usd"),
        )
        if self.max_attempts is not None:
            _validate_max_attempts(self.max_attempts)
        if self.backoff_base_s is not None:
            _validate_backoff_base_s(self.backoff_base_s)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.scenario_id,
            "description": self.description,
            "price_override_provider_ids": sorted(self.price_overrides),
            "provider_override_provider_ids": sorted(self.provider_overrides),
            "removed_provider_ids": sorted(self.removed_provider_ids),
            "additional_provider_ids": sorted(
                _provider_id(provider) for provider in self.additional_providers
            ),
            "availability_override": (
                self.availability_snapshot is not None or bool(self.availability_entries)
            ),
            "has_hints_override": self.hints is not None,
            "has_observed_override": self.observed is not None,
            "budget_usd": _optional_decimal_to_str(self.budget_usd),
            "current_spend_usd": _optional_decimal_to_str(self.current_spend_usd),
            "max_attempts": self.max_attempts,
            "backoff_base_s": self.backoff_base_s,
        }


@dataclass(frozen=True, slots=True)
class TimeMachineWorkloadReport:
    """Counterfactual route/cost comparison for one historical workload."""

    workload_id: str
    actual_provider_id: str | None
    actual_fallback_chain: tuple[str, ...]
    actual_cost_usd: Decimal
    counterfactual_provider_id: str | None
    counterfactual_fallback_chain: tuple[str, ...]
    counterfactual_cost_usd: Decimal
    cost_delta_usd: Decimal
    routed_differently: bool
    projection: WhatIfProjection

    def to_dict(self) -> dict[str, object]:
        return {
            "workload_id": self.workload_id,
            "actual": {
                "provider_id": self.actual_provider_id,
                "fallback_chain": list(self.actual_fallback_chain),
                "cost_usd": _decimal_to_str(self.actual_cost_usd),
            },
            "counterfactual": {
                "provider_id": self.counterfactual_provider_id,
                "fallback_chain": list(self.counterfactual_fallback_chain),
                "cost_usd": _decimal_to_str(self.counterfactual_cost_usd),
            },
            "delta": {
                "cost_usd": _decimal_to_str(self.cost_delta_usd),
                "routed_differently": self.routed_differently,
            },
            "projection": self.projection.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class TimeMachineSummary:
    """Aggregate counterfactual report totals."""

    workload_count: int
    changed_route_count: int
    unroutable_count: int
    actual_total_usd: Decimal
    counterfactual_total_usd: Decimal
    cost_delta_usd: Decimal
    actual_provider_counts: Mapping[str, int] = field(default_factory=dict)
    counterfactual_provider_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.workload_count, "workload_count")
        _validate_non_negative_int(self.changed_route_count, "changed_route_count")
        _validate_non_negative_int(self.unroutable_count, "unroutable_count")
        object.__setattr__(
            self, "actual_total_usd", _usd(self.actual_total_usd, "actual_total_usd")
        )
        object.__setattr__(
            self,
            "counterfactual_total_usd",
            _usd(self.counterfactual_total_usd, "counterfactual_total_usd"),
        )
        object.__setattr__(
            self,
            "cost_delta_usd",
            _signed_usd(self.cost_delta_usd, "cost_delta_usd"),
        )
        object.__setattr__(
            self,
            "actual_provider_counts",
            _freeze_counts(self.actual_provider_counts, "actual_provider_counts"),
        )
        object.__setattr__(
            self,
            "counterfactual_provider_counts",
            _freeze_counts(
                self.counterfactual_provider_counts,
                "counterfactual_provider_counts",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "workload_count": self.workload_count,
            "changed_route_count": self.changed_route_count,
            "unroutable_count": self.unroutable_count,
            "actual_total_usd": _decimal_to_str(self.actual_total_usd),
            "counterfactual_total_usd": _decimal_to_str(self.counterfactual_total_usd),
            "cost_delta_usd": _decimal_to_str(self.cost_delta_usd),
            "actual_provider_counts": dict(self.actual_provider_counts),
            "counterfactual_provider_counts": dict(self.counterfactual_provider_counts),
        }


@dataclass(frozen=True, slots=True)
class TimeMachineReport:
    """Full counterfactual report for a scenario."""

    scenario: CounterfactualScenario
    workloads: tuple[TimeMachineWorkloadReport, ...]
    summary: TimeMachineSummary

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario.to_dict(),
            "summary": self.summary.to_dict(),
            "workloads": [workload.to_dict() for workload in self.workloads],
        }


class TimeMachineReplay:
    """Replay historical routing decisions through counterfactual scenarios."""

    def __init__(self, historical_context: PlanningContext) -> None:
        self._historical_context = historical_context

    def replay(
        self,
        *,
        scenario: CounterfactualScenario,
        decisions: Iterable[HistoricalRoutingDecision],
    ) -> TimeMachineReport:
        """Return a deterministic report comparing counterfactuals to actuals."""

        replay_context = _context_for_scenario(self._historical_context, scenario)
        simulator = WhatIfSimulator(
            replay_context,
            price_overrides=scenario.price_overrides,
            budget_usd=scenario.budget_usd,
            current_spend_usd=scenario.current_spend_usd,
            max_attempts=(
                scenario.max_attempts if scenario.max_attempts is not None else DEFAULT_MAX_ATTEMPTS
            ),
            backoff_base_s=(
                scenario.backoff_base_s
                if scenario.backoff_base_s is not None
                else DEFAULT_BACKOFF_BASE_S
            ),
        )
        workload_reports = tuple(
            _workload_report(
                decision=decision,
                scenario=scenario,
                replay_context=replay_context,
                simulator=simulator,
            )
            for decision in decisions
        )
        return TimeMachineReport(
            scenario=scenario,
            workloads=workload_reports,
            summary=_summary(workload_reports),
        )


def _workload_report(
    *,
    decision: HistoricalRoutingDecision,
    scenario: CounterfactualScenario,
    replay_context: PlanningContext,
    simulator: WhatIfSimulator,
) -> TimeMachineWorkloadReport:
    projection = (
        simulator.simulate(
            decision.request,
            payload=decision.payload,
            hints=scenario.hints,
            observed=scenario.observed,
            max_attempts=scenario.max_attempts,
            backoff_base_s=scenario.backoff_base_s,
        )
        if replay_context.providers
        else _empty_projection(decision.request, scenario=scenario)
    )
    counterfactual_provider_id = projection.plan.selected_provider_id
    counterfactual_fallback_chain = projection.plan.fallback_chain
    routed_differently = (
        decision.actual_provider_id != counterfactual_provider_id
        or decision.actual_fallback_chain != counterfactual_fallback_chain
    )
    counterfactual_cost = projection.reserved_usd
    return TimeMachineWorkloadReport(
        workload_id=decision.workload_id,
        actual_provider_id=decision.actual_provider_id,
        actual_fallback_chain=decision.actual_fallback_chain,
        actual_cost_usd=decision.actual_cost_usd,
        counterfactual_provider_id=counterfactual_provider_id,
        counterfactual_fallback_chain=counterfactual_fallback_chain,
        counterfactual_cost_usd=counterfactual_cost,
        cost_delta_usd=_signed_usd(
            counterfactual_cost - decision.actual_cost_usd,
            "cost_delta_usd",
        ),
        routed_differently=routed_differently,
        projection=projection,
    )


def _summary(workloads: Sequence[TimeMachineWorkloadReport]) -> TimeMachineSummary:
    actual_total = _usd(
        sum((workload.actual_cost_usd for workload in workloads), _ZERO_USD),
        "actual_total_usd",
    )
    counterfactual_total = _usd(
        sum((workload.counterfactual_cost_usd for workload in workloads), _ZERO_USD),
        "counterfactual_total_usd",
    )
    return TimeMachineSummary(
        workload_count=len(workloads),
        changed_route_count=sum(1 for workload in workloads if workload.routed_differently),
        unroutable_count=sum(
            1 for workload in workloads if workload.counterfactual_provider_id is None
        ),
        actual_total_usd=actual_total,
        counterfactual_total_usd=counterfactual_total,
        cost_delta_usd=_signed_usd(
            counterfactual_total - actual_total,
            "cost_delta_usd",
        ),
        actual_provider_counts=_provider_counts(
            workload.actual_provider_id for workload in workloads
        ),
        counterfactual_provider_counts=_provider_counts(
            workload.counterfactual_provider_id for workload in workloads
        ),
    )


def _context_for_scenario(
    context: PlanningContext,
    scenario: CounterfactualScenario,
) -> PlanningContext:
    providers = _providers_for_scenario(context, scenario)
    return PlanningContext.replay(
        now=context.now,
        availability_snapshot=_availability_for_scenario(context, scenario),
        providers=providers,
        capability=context.capability,
    )


def _providers_for_scenario(
    context: PlanningContext,
    scenario: CounterfactualScenario,
) -> tuple[ProviderSnapshot, ...]:
    removed = set(scenario.removed_provider_ids)
    providers_by_id: dict[str, dict[str, Any]] = {}
    for provider in (*context.providers, *scenario.additional_providers):
        provider_copy = _mutable_provider(provider)
        provider_id = _provider_id(provider_copy)
        if provider_id in removed:
            continue
        if provider_id in providers_by_id:
            raise ValueError(f"provider {provider_id!r} appears more than once")
        providers_by_id[provider_id] = provider_copy

    for provider_id, override in scenario.provider_overrides.items():
        if provider_id not in providers_by_id:
            raise ValueError(f"provider override {provider_id!r} did not match a provider")
        providers_by_id[provider_id] = _merge_mapping(providers_by_id[provider_id], override)

    return freeze_provider_snapshot(providers_by_id.values())


def _availability_for_scenario(
    context: PlanningContext,
    scenario: CounterfactualScenario,
) -> AvailabilitySnapshot:
    if scenario.availability_snapshot is not None:
        return scenario.availability_snapshot
    if scenario.availability_entries:
        return AvailabilitySnapshot.from_entries(scenario.availability_entries)
    return context.availability_snapshot


def _empty_projection(
    request: RoutingRequest,
    *,
    scenario: CounterfactualScenario,
) -> WhatIfProjection:
    current_spend = scenario.current_spend_usd or _ZERO_USD
    budget_headroom = _budget_headroom(
        budget_usd=scenario.budget_usd,
        projected_spend_usd=current_spend,
    )
    return WhatIfProjection(
        plan=RoutePlan(
            request=request,
            max_attempts=(
                scenario.max_attempts if scenario.max_attempts is not None else DEFAULT_MAX_ATTEMPTS
            ),
        ),
        attempt_costs=(),
        reserved_usd=_ZERO_USD,
        current_spend_usd=current_spend,
        projected_spend_usd=current_spend,
        budget_usd=scenario.budget_usd,
        budget_headroom_usd=budget_headroom,
        would_exceed_budget=None if budget_headroom is None else budget_headroom < _ZERO_USD,
    )


def _merge_mapping(base: Mapping[str, Any], override: Mapping[str, object]) -> dict[str, Any]:
    merged = _mutable_mapping(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_mapping(
                cast(Mapping[str, Any], existing),
                cast(Mapping[str, object], value),
            )
        else:
            merged[key] = _thaw_value(value)
    return merged


def _mutable_provider(provider: ProviderInput) -> dict[str, Any]:
    if isinstance(provider, Mapping):
        return _mutable_mapping(provider)
    return _mutable_mapping(provider.model_dump(mode="python"))


def _mutable_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _thaw_value(value) for key, value in mapping.items()}


def _thaw_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return _mutable_mapping(cast(Mapping[str, Any], value))
    if _is_sequence(value):
        return tuple(_thaw_value(item) for item in cast(Sequence[object], value))
    return value


def _freeze_provider_overrides(
    overrides: Mapping[str, Mapping[str, object]],
) -> Mapping[str, Mapping[str, object]]:
    return MappingProxyType(
        {
            _non_empty_string(provider_id, "provider_id"): _freeze_mapping(override)
            for provider_id, override in overrides.items()
        }
    )


def _freeze_mapping(mapping: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({str(key): _freeze_value(value) for key, value in mapping.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(cast(Mapping[str, object], value))
    if isinstance(value, Enum):
        return value
    if _is_sequence(value):
        return tuple(_freeze_value(item) for item in cast(Sequence[object], value))
    return value


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str))


def _provider_counts(provider_ids: Iterable[str | None]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for provider_id in provider_ids:
        if provider_id is None:
            continue
        counts[provider_id] = counts.get(provider_id, 0) + 1
    return MappingProxyType(dict(sorted(counts.items())))


def _freeze_counts(counts: Mapping[str, int], name: str) -> Mapping[str, int]:
    frozen: dict[str, int] = {}
    for key, value in counts.items():
        provider_id = _non_empty_string(key, f"{name} key")
        _validate_non_negative_int(value, f"{name}[{provider_id}]")
        frozen[provider_id] = value
    return MappingProxyType(dict(sorted(frozen.items())))


def _fallback_chain(values: Sequence[str], *, actual_provider_id: str | None) -> tuple[str, ...]:
    chain = _string_tuple(values, "actual_fallback_chain")
    if chain:
        return chain
    if actual_provider_id is None:
        return ()
    return (actual_provider_id,)


def _string_tuple(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be a sequence of strings")
    return tuple(_non_empty_string(value, name) for value in values)


def _provider_id(provider: ProviderInput) -> str:
    value: object = provider.get("id") if isinstance(provider, Mapping) else provider.id
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and value:
        return value
    raise ValueError("provider must include a non-empty id")


def _non_empty_string(value: object, name: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"{name} must be a non-empty string")


def _optional_non_empty_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, name)


def _validate_non_negative_int(value: int, name: str) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_max_attempts(value: int) -> None:
    if isinstance(value, bool) or value < 1:
        raise ValueError("max_attempts must be >= 1")


def _validate_backoff_base_s(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError("backoff_base_s must be a finite non-negative number")


def _budget_headroom(
    *,
    budget_usd: Decimal | None,
    projected_spend_usd: Decimal,
) -> Decimal | None:
    if budget_usd is None:
        return None
    return _signed_usd(budget_usd - projected_spend_usd, "budget_headroom_usd")


def _optional_usd(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    return _usd(value, name)


def _usd(value: object, name: str) -> Decimal:
    decimal_value = _decimal(value, name)
    if decimal_value < 0:
        raise ValueError(f"{name} must be non-negative")
    return _quantize_usd(decimal_value, name)


def _signed_usd(value: object, name: str) -> Decimal:
    return _quantize_usd(_decimal(value, name), name)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal value")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal value") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"{name} must be finite")
    return decimal_value


def _quantize_usd(value: Decimal, name: str) -> Decimal:
    try:
        return value.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"{name} is out of representable USD range: {value}") from exc


def _decimal_to_str(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _decimal_to_str(value)


__all__ = [
    "CounterfactualScenario",
    "HistoricalRoutingDecision",
    "ObservedByProvider",
    "TimeMachineReport",
    "TimeMachineReplay",
    "TimeMachineSummary",
    "TimeMachineWorkloadReport",
]
