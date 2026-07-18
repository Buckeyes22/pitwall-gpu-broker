"""Pure what-if route and cost projection for FinOps workflows."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, cast

from pitwall.core.models import Capability
from pitwall.cost.estimator import EstimatePayload, quote_cost
from pitwall.routing.context import AvailabilitySnapshot, PlanningContext, ProviderInput
from pitwall.routing.planner import (
    DEFAULT_BACKOFF_BASE_S,
    DEFAULT_MAX_ATTEMPTS,
    plan_route,
)
from pitwall.routing.types import Hints, ObservedMetrics, RoutePlan, RoutingRequest

_USD_QUANTUM = Decimal("0.000001")

type AvailabilityEntry = tuple[str, str, str, int, bool]
type PriceOverrides = Mapping[str, object]
type ObservedByProvider = Mapping[str, ObservedMetrics | Mapping[str, object] | float | int]


@dataclass(frozen=True, slots=True)
class WhatIfWorkload:
    """One hypothetical workload to replay through the planner."""

    request: RoutingRequest
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderCostProjection:
    """Cost quote for one provider in the planned attempt chain."""

    provider_id: str
    attempt: int
    estimate_usd: Decimal
    upper_bound_usd: Decimal
    pricing_kind: str
    selected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "attempt": self.attempt,
            "estimate_usd": _decimal_to_str(self.estimate_usd),
            "upper_bound_usd": _decimal_to_str(self.upper_bound_usd),
            "pricing_kind": self.pricing_kind,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class WhatIfProjection:
    """Projected route, selected-provider reservation, and budget headroom."""

    plan: RoutePlan
    attempt_costs: tuple[ProviderCostProjection, ...]
    reserved_usd: Decimal
    current_spend_usd: Decimal
    projected_spend_usd: Decimal
    budget_usd: Decimal | None
    budget_headroom_usd: Decimal | None
    would_exceed_budget: bool | None

    @property
    def selected_cost(self) -> ProviderCostProjection | None:
        for attempt_cost in self.attempt_costs:
            if attempt_cost.selected:
                return attempt_cost
        return None

    def to_dict(self) -> dict[str, Any]:
        selected = self.selected_cost
        return {
            "plan": self.plan.to_dict(),
            "cost": {
                "attempts": [attempt.to_dict() for attempt in self.attempt_costs],
                "selected": selected.to_dict() if selected is not None else None,
                "reserved_usd": _decimal_to_str(self.reserved_usd),
                "current_spend_usd": _decimal_to_str(self.current_spend_usd),
                "projected_spend_usd": _decimal_to_str(self.projected_spend_usd),
                "budget_usd": _optional_decimal_to_str(self.budget_usd),
                "budget_headroom_usd": _optional_decimal_to_str(self.budget_headroom_usd),
                "would_exceed_budget": self.would_exceed_budget,
            },
        }


@dataclass(frozen=True, slots=True)
class WhatIfBatchProjection:
    """Aggregate projection for a sequence of hypothetical workloads."""

    projections: tuple[WhatIfProjection, ...]
    total_reserved_usd: Decimal
    starting_spend_usd: Decimal
    projected_spend_usd: Decimal
    budget_usd: Decimal | None
    budget_headroom_usd: Decimal | None
    would_exceed_budget: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "projections": [projection.to_dict() for projection in self.projections],
            "cost": {
                "total_reserved_usd": _decimal_to_str(self.total_reserved_usd),
                "starting_spend_usd": _decimal_to_str(self.starting_spend_usd),
                "projected_spend_usd": _decimal_to_str(self.projected_spend_usd),
                "budget_usd": _optional_decimal_to_str(self.budget_usd),
                "budget_headroom_usd": _optional_decimal_to_str(self.budget_headroom_usd),
                "would_exceed_budget": self.would_exceed_budget,
            },
        }


class WhatIfSimulator:
    """Replay the pure planner against hypothetical price and budget inputs."""

    def __init__(
        self,
        context: PlanningContext,
        *,
        price_overrides: PriceOverrides | None = None,
        budget_usd: object = None,
        current_spend_usd: object = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    ) -> None:
        self._base_context = context
        self._price_overrides = dict(price_overrides or {})
        self._budget_usd = _optional_usd(budget_usd, "budget_usd")
        self._current_spend_usd = _usd_or_zero(current_spend_usd, "current_spend_usd")
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s

    @classmethod
    def from_inputs(
        cls,
        *,
        now: dt.datetime,
        providers: Iterable[ProviderInput],
        capability: Capability | None = None,
        availability_snapshot: AvailabilitySnapshot | None = None,
        availability_entries: Iterable[AvailabilityEntry] = (),
        price_overrides: PriceOverrides | None = None,
        budget_usd: object = None,
        current_spend_usd: object = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    ) -> WhatIfSimulator:
        context = PlanningContext.replay(
            now=now,
            providers=providers,
            capability=capability,
            availability_snapshot=availability_snapshot,
            availability_entries=availability_entries,
        )
        return cls(
            context,
            price_overrides=price_overrides,
            budget_usd=budget_usd,
            current_spend_usd=current_spend_usd,
            max_attempts=max_attempts,
            backoff_base_s=backoff_base_s,
        )

    def simulate(
        self,
        request: RoutingRequest,
        *,
        payload: Mapping[str, Any] | None = None,
        capability: Capability | None = None,
        hints: Hints | None = None,
        observed: ObservedMetrics | ObservedByProvider | None = None,
        price_overrides: PriceOverrides | None = None,
        budget_usd: object = None,
        current_spend_usd: object = None,
        max_attempts: int | None = None,
        backoff_base_s: float | None = None,
    ) -> WhatIfProjection:
        context = self._context_for(price_overrides)
        resolved_capability = self._resolve_capability(capability, context)
        resolved_budget = self._resolve_budget(budget_usd)
        resolved_current_spend = self._resolve_current_spend(current_spend_usd)
        plan = plan_route(
            request,
            context=context,
            capability=resolved_capability,
            hints=hints,
            observed=observed,
            max_attempts=max_attempts if max_attempts is not None else self._max_attempts,
            backoff_base_s=(backoff_base_s if backoff_base_s is not None else self._backoff_base_s),
        )
        attempt_costs = _attempt_costs(
            plan,
            capability=resolved_capability,
            payload=dict(payload or {}),
        )
        reserved_usd = attempt_costs[0].upper_bound_usd if attempt_costs else Decimal("0.000000")
        projected_spend = _usd(resolved_current_spend + reserved_usd, "projected_spend_usd")
        budget_headroom = _budget_headroom(
            budget_usd=resolved_budget,
            projected_spend_usd=projected_spend,
        )
        return WhatIfProjection(
            plan=plan,
            attempt_costs=attempt_costs,
            reserved_usd=reserved_usd,
            current_spend_usd=resolved_current_spend,
            projected_spend_usd=projected_spend,
            budget_usd=resolved_budget,
            budget_headroom_usd=budget_headroom,
            would_exceed_budget=(
                None if budget_headroom is None else budget_headroom < Decimal("0")
            ),
        )

    def simulate_workloads(
        self,
        workloads: Iterable[WhatIfWorkload],
        *,
        price_overrides: PriceOverrides | None = None,
        budget_usd: object = None,
        current_spend_usd: object = None,
    ) -> WhatIfBatchProjection:
        resolved_budget = self._resolve_budget(budget_usd)
        running_spend = self._resolve_current_spend(current_spend_usd)
        starting_spend = running_spend
        projections: list[WhatIfProjection] = []

        for workload in workloads:
            projection = self.simulate(
                workload.request,
                payload=workload.payload,
                price_overrides=price_overrides,
                budget_usd=resolved_budget,
                current_spend_usd=running_spend,
            )
            projections.append(projection)
            running_spend = projection.projected_spend_usd

        total_reserved = _usd(
            sum(
                (projection.reserved_usd for projection in projections),
                Decimal("0"),
            ),
            "total_reserved_usd",
        )
        budget_headroom = _budget_headroom(
            budget_usd=resolved_budget,
            projected_spend_usd=running_spend,
        )
        return WhatIfBatchProjection(
            projections=tuple(projections),
            total_reserved_usd=total_reserved,
            starting_spend_usd=starting_spend,
            projected_spend_usd=running_spend,
            budget_usd=resolved_budget,
            budget_headroom_usd=budget_headroom,
            would_exceed_budget=(
                None if budget_headroom is None else budget_headroom < Decimal("0")
            ),
        )

    def _context_for(self, price_overrides: PriceOverrides | None) -> PlanningContext:
        merged_overrides: dict[str, object] = dict(self._price_overrides)
        if price_overrides is not None:
            merged_overrides.update(price_overrides)
        if not merged_overrides:
            return self._base_context
        return _context_with_price_overrides(self._base_context, merged_overrides)

    def _resolve_budget(self, budget_usd: object) -> Decimal | None:
        if budget_usd is None:
            return self._budget_usd
        return _optional_usd(budget_usd, "budget_usd")

    def _resolve_current_spend(self, current_spend_usd: object) -> Decimal:
        if current_spend_usd is None:
            return self._current_spend_usd
        return _usd(current_spend_usd, "current_spend_usd")

    @staticmethod
    def _resolve_capability(
        capability: Capability | None,
        context: PlanningContext,
    ) -> Capability:
        if capability is not None:
            return capability
        if context.capability is not None:
            return context.capability
        raise ValueError("capability must be supplied directly or through PlanningContext")


def _attempt_costs(
    plan: RoutePlan,
    *,
    capability: Capability,
    payload: EstimatePayload,
) -> tuple[ProviderCostProjection, ...]:
    costs: list[ProviderCostProjection] = []
    for attempt in plan.attempts:
        quote = quote_cost(
            capability=capability,
            provider_cost=attempt.provider,
            payload=payload,
        )
        costs.append(
            ProviderCostProjection(
                provider_id=attempt.provider_id,
                attempt=attempt.attempt,
                estimate_usd=quote.estimate(),
                upper_bound_usd=quote.upper_bound(),
                pricing_kind=quote.pricing.kind,
                selected=attempt.attempt == 1,
            )
        )
    return tuple(costs)


def _context_with_price_overrides(
    context: PlanningContext,
    price_overrides: PriceOverrides,
) -> PlanningContext:
    providers = tuple(
        _provider_with_price_override(provider, price_overrides) for provider in context.providers
    )
    return PlanningContext.replay(
        now=context.now,
        availability_snapshot=context.availability_snapshot,
        providers=providers,
        capability=context.capability,
    )


def _provider_with_price_override(
    provider: Mapping[str, Any],
    price_overrides: PriceOverrides,
) -> Mapping[str, Any]:
    provider_id = _provider_id(provider)
    override = price_overrides.get(provider_id)
    if override is None:
        return provider

    provider_copy = _mutable_mapping(provider)
    override_cost = _override_cost_mapping(provider_id, override)
    config = _provider_config(provider_copy)
    cost_copy = _mutable_mapping(override_cost)
    provider_copy["cost"] = cost_copy
    config["cost"] = cost_copy

    score_rate = _score_rate(cost_copy)
    if score_rate is not None:
        score_value = str(score_rate)
        provider_copy["cost_per_second_active"] = score_value
        provider_copy["per_second_active"] = score_value
        config["cost_per_second_active"] = score_value
        config["per_second_active"] = score_value

    return provider_copy


def _provider_config(provider: dict[str, Any]) -> dict[str, Any]:
    raw_config = provider.get("config")
    config = _mutable_mapping(raw_config) if isinstance(raw_config, Mapping) else {}
    provider["config"] = config
    return config


def _override_cost_mapping(provider_id: str, override: object) -> Mapping[str, Any]:
    if not isinstance(override, Mapping):
        raise ValueError(f"price override for provider {provider_id!r} must be a mapping")

    config = override.get("config")
    if isinstance(config, Mapping):
        config_cost = config.get("cost")
        if isinstance(config_cost, Mapping):
            return cast(Mapping[str, Any], config_cost)

    cost = override.get("cost")
    if isinstance(cost, Mapping):
        return cast(Mapping[str, Any], cost)

    return cast(Mapping[str, Any], override)


def _score_rate(cost: Mapping[str, Any]) -> Decimal | None:
    if "per_second_active" in cost:
        return _decimal(cost["per_second_active"], "per_second_active")

    raw_kind = cost.get("kind", cost.get("model"))
    kind = _string_value(raw_kind)
    if kind in {"per_second", "per_vm_second"} and "rate_per_second" in cost:
        return _decimal(cost["rate_per_second"], "rate_per_second")
    return None


def _provider_id(provider: Mapping[str, Any]) -> str:
    value = provider.get("id")
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and value:
        return value
    raise ValueError("provider must include a non-empty id")


def _mutable_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _thaw_value(value) for key, value in mapping.items()}


def _thaw_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _mutable_mapping(cast(Mapping[str, Any], value))
    if _is_sequence(value):
        return tuple(_thaw_value(item) for item in cast(Sequence[object], value))
    return value


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str))


def _budget_headroom(
    *,
    budget_usd: Decimal | None,
    projected_spend_usd: Decimal,
) -> Decimal | None:
    if budget_usd is None:
        return None
    return _signed_usd(budget_usd - projected_spend_usd, "budget_headroom_usd")


def _usd_or_zero(value: object, name: str) -> Decimal:
    if value is None:
        return Decimal("0.000000")
    return _usd(value, name)


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


def _string_value(value: object) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "AvailabilityEntry",
    "ProviderCostProjection",
    "WhatIfBatchProjection",
    "WhatIfProjection",
    "WhatIfSimulator",
    "WhatIfWorkload",
]
