"""Reservation and warm-pool sizing recommendations for Pitwall FinOps."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal, cast

from pitwall.cost.simulator import (
    WhatIfBatchProjection,
    WhatIfProjection,
    WhatIfSimulator,
    WhatIfWorkload,
)
from pitwall.finops.burn_rate import BurnRateForecast

_USD_QUANTUM = Decimal("0.000001")

RecommendationAction = Literal["reserve", "on_demand", "blocked"]


@dataclass(frozen=True, slots=True)
class DemandForecast:
    """Forecasted demand replayed through the what-if simulator."""

    name: str
    workloads: tuple[WhatIfWorkload, ...]
    window_hours: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("demand forecast name must be non-empty")
        object.__setattr__(self, "workloads", tuple(self.workloads))
        object.__setattr__(
            self,
            "window_hours",
            _non_negative_decimal(self.window_hours, "window_hours"),
        )

    @property
    def workload_count(self) -> int:
        return len(self.workloads)

    def to_dict(self) -> dict[str, str | int]:
        return {
            "name": self.name,
            "workload_count": self.workload_count,
            "window_hours": _decimal_to_str(self.window_hours),
        }


@dataclass(frozen=True, slots=True)
class ReservationLine:
    """One provider reservation or warm-pool sizing line."""

    provider_id: str
    reserved_units: int = 0
    warm_pool_size: int = 0
    unit_capacity: int = 1
    hourly_commitment_usd: Decimal = Decimal("0")
    upfront_usd: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id must be non-empty")
        _validate_non_negative_int(self.reserved_units, "reserved_units")
        _validate_non_negative_int(self.warm_pool_size, "warm_pool_size")
        if self.unit_capacity < 1:
            raise ValueError("unit_capacity must be >= 1")
        if self.total_units < 1:
            raise ValueError("reservation line must include reserved_units or warm_pool_size")
        object.__setattr__(
            self,
            "hourly_commitment_usd",
            _usd(self.hourly_commitment_usd, "hourly_commitment_usd"),
        )
        object.__setattr__(self, "upfront_usd", _usd(self.upfront_usd, "upfront_usd"))

    @property
    def total_units(self) -> int:
        return self.reserved_units + self.warm_pool_size

    @property
    def capacity_workloads(self) -> int:
        return self.total_units * self.unit_capacity

    def fixed_cost_usd(self, window_hours: Decimal) -> Decimal:
        return _usd(
            self.upfront_usd
            + self.hourly_commitment_usd * Decimal(self.total_units) * window_hours,
            "fixed_cost_usd",
        )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "provider_id": self.provider_id,
            "reserved_units": self.reserved_units,
            "warm_pool_size": self.warm_pool_size,
            "unit_capacity": self.unit_capacity,
            "capacity_workloads": self.capacity_workloads,
            "hourly_commitment_usd": _decimal_to_str(self.hourly_commitment_usd),
            "upfront_usd": _decimal_to_str(self.upfront_usd),
        }


@dataclass(frozen=True, slots=True)
class ReservationCandidate:
    """A candidate build/warm-pool plan evaluated against on-demand routing."""

    plan_id: str
    reserves: tuple[ReservationLine, ...] = ()
    price_overrides: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("plan_id must be non-empty")
        reserves = tuple(self.reserves)
        object.__setattr__(self, "reserves", reserves)
        frozen_overrides = _freeze_mapping(self.price_overrides)
        object.__setattr__(self, "price_overrides", frozen_overrides)
        override_provider_ids = set(frozen_overrides)
        reserve_provider_ids = {line.provider_id for line in reserves}
        if not override_provider_ids <= reserve_provider_ids:
            raise ValueError("price_overrides must reference reserved providers")

    @property
    def capacity_by_provider(self) -> dict[str, int]:
        capacity: dict[str, int] = {}
        for line in self.reserves:
            capacity[line.provider_id] = capacity.get(line.provider_id, 0) + line.capacity_workloads
        return capacity

    def fixed_cost_usd(self, window_hours: Decimal) -> Decimal:
        return _usd(
            sum((line.fixed_cost_usd(window_hours) for line in self.reserves), Decimal("0")),
            "fixed_cost_usd",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "reserves": [line.to_dict() for line in self.reserves],
            "price_override_provider_ids": sorted(self.price_overrides),
        }


@dataclass(frozen=True, slots=True)
class PlanEvaluation:
    """Cost and demand-fit result for one reservation candidate."""

    plan: ReservationCandidate
    fixed_cost_usd: Decimal
    marginal_cost_usd: Decimal
    total_cost_usd: Decimal
    projected_savings_usd: Decimal
    covered_workloads: int
    on_demand_overflow_workloads: int
    unmet_workloads: int
    meets_demand: bool
    selected_provider_counts: Mapping[str, int] = field(default_factory=dict)
    budget_after_plan_usd: Decimal | None = None
    runway_days_after_plan: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixed_cost_usd", _usd(self.fixed_cost_usd, "fixed_cost_usd"))
        object.__setattr__(
            self,
            "marginal_cost_usd",
            _usd(self.marginal_cost_usd, "marginal_cost_usd"),
        )
        object.__setattr__(self, "total_cost_usd", _usd(self.total_cost_usd, "total_cost_usd"))
        object.__setattr__(
            self,
            "projected_savings_usd",
            _signed_usd(self.projected_savings_usd, "projected_savings_usd"),
        )
        _validate_non_negative_int(self.covered_workloads, "covered_workloads")
        _validate_non_negative_int(
            self.on_demand_overflow_workloads,
            "on_demand_overflow_workloads",
        )
        _validate_non_negative_int(self.unmet_workloads, "unmet_workloads")
        object.__setattr__(
            self,
            "selected_provider_counts",
            MappingProxyType(dict(sorted(self.selected_provider_counts.items()))),
        )
        if self.budget_after_plan_usd is not None:
            object.__setattr__(
                self,
                "budget_after_plan_usd",
                _signed_usd(self.budget_after_plan_usd, "budget_after_plan_usd"),
            )
        if self.runway_days_after_plan is not None:
            object.__setattr__(
                self,
                "runway_days_after_plan",
                _non_negative_decimal(self.runway_days_after_plan, "runway_days_after_plan"),
            )

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "fixed_cost_usd": _decimal_to_str(self.fixed_cost_usd),
            "marginal_cost_usd": _decimal_to_str(self.marginal_cost_usd),
            "total_cost_usd": _decimal_to_str(self.total_cost_usd),
            "projected_savings_usd": _decimal_to_str(self.projected_savings_usd),
            "covered_workloads": self.covered_workloads,
            "on_demand_overflow_workloads": self.on_demand_overflow_workloads,
            "unmet_workloads": self.unmet_workloads,
            "meets_demand": self.meets_demand,
            "selected_provider_counts": dict(self.selected_provider_counts),
            "budget_after_plan_usd": _optional_decimal_to_str(self.budget_after_plan_usd),
            "runway_days_after_plan": _optional_decimal_to_str(self.runway_days_after_plan),
        }


@dataclass(frozen=True, slots=True)
class ReservationRecommendation:
    """Structured recommendation for on-demand vs reserved/warm capacity."""

    demand: DemandForecast
    baseline: PlanEvaluation
    evaluations: tuple[PlanEvaluation, ...]
    recommended: PlanEvaluation
    action: RecommendationAction
    burn_rate_forecast: BurnRateForecast | None = None

    @property
    def projected_savings_usd(self) -> Decimal:
        return self.recommended.projected_savings_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "demand": self.demand.to_dict(),
            "action": self.action,
            "baseline": self.baseline.to_dict(),
            "evaluations": [evaluation.to_dict() for evaluation in self.evaluations],
            "recommended": self.recommended.to_dict(),
            "projected_savings_usd": _decimal_to_str(self.projected_savings_usd),
            "burn_rate_forecast": _burn_rate_to_dict(self.burn_rate_forecast),
        }


@dataclass(frozen=True, slots=True)
class ReservationRecommender:
    """Reusable recommender bound to one what-if simulator."""

    simulator: WhatIfSimulator

    def recommend(
        self,
        *,
        demand: DemandForecast,
        candidates: Iterable[ReservationCandidate],
        burn_rate_forecast: BurnRateForecast | None = None,
    ) -> ReservationRecommendation:
        return recommend_reservations(
            demand=demand,
            simulator=self.simulator,
            candidates=candidates,
            burn_rate_forecast=burn_rate_forecast,
        )


def recommend_reservations(
    *,
    demand: DemandForecast,
    simulator: WhatIfSimulator,
    candidates: Iterable[ReservationCandidate],
    burn_rate_forecast: BurnRateForecast | None = None,
) -> ReservationRecommendation:
    """Evaluate candidate reservations and return the lowest-cost recommendation."""

    candidate_tuple = tuple(candidates)
    baseline_batch = simulator.simulate_workloads(demand.workloads)
    baseline = _baseline_evaluation(
        demand=demand,
        baseline_batch=baseline_batch,
        burn_rate_forecast=burn_rate_forecast,
    )
    evaluations = tuple(
        _evaluate_candidate(
            demand=demand,
            simulator=simulator,
            candidate=candidate,
            baseline_projections=baseline_batch.projections,
            baseline_total_usd=baseline.total_cost_usd,
            burn_rate_forecast=burn_rate_forecast,
        )
        for candidate in candidate_tuple
    )

    eligible = tuple(
        evaluation for evaluation in (baseline, *evaluations) if evaluation.meets_demand
    )
    if not eligible:
        return ReservationRecommendation(
            demand=demand,
            baseline=baseline,
            evaluations=evaluations,
            recommended=baseline,
            action="blocked",
            burn_rate_forecast=burn_rate_forecast,
        )

    recommended = min(eligible, key=_recommendation_sort_key)
    action: RecommendationAction = "on_demand" if recommended.plan_id == "on_demand" else "reserve"
    return ReservationRecommendation(
        demand=demand,
        baseline=baseline,
        evaluations=evaluations,
        recommended=recommended,
        action=action,
        burn_rate_forecast=burn_rate_forecast,
    )


def _baseline_evaluation(
    *,
    demand: DemandForecast,
    baseline_batch: WhatIfBatchProjection,
    burn_rate_forecast: BurnRateForecast | None,
) -> PlanEvaluation:
    unmet = sum(1 for projection in baseline_batch.projections if projection.selected_cost is None)
    selected_counts = _selected_provider_counts(baseline_batch.projections)
    budget_after, runway_after = _budget_metadata(
        total_cost_usd=baseline_batch.total_reserved_usd,
        burn_rate_forecast=burn_rate_forecast,
    )
    return PlanEvaluation(
        plan=ReservationCandidate(plan_id="on_demand"),
        fixed_cost_usd=Decimal("0"),
        marginal_cost_usd=baseline_batch.total_reserved_usd,
        total_cost_usd=baseline_batch.total_reserved_usd,
        projected_savings_usd=Decimal("0"),
        covered_workloads=0,
        on_demand_overflow_workloads=demand.workload_count - unmet,
        unmet_workloads=unmet,
        meets_demand=unmet == 0,
        selected_provider_counts=selected_counts,
        budget_after_plan_usd=budget_after,
        runway_days_after_plan=runway_after,
    )


def _evaluate_candidate(
    *,
    demand: DemandForecast,
    simulator: WhatIfSimulator,
    candidate: ReservationCandidate,
    baseline_projections: Sequence[WhatIfProjection],
    baseline_total_usd: Decimal,
    burn_rate_forecast: BurnRateForecast | None,
) -> PlanEvaluation:
    remaining_capacity = candidate.capacity_by_provider
    fixed_cost = candidate.fixed_cost_usd(demand.window_hours)
    marginal_cost = Decimal("0")
    covered = 0
    overflow = 0
    unmet = 0
    selected_counts: dict[str, int] = {}

    for workload, baseline_projection in zip(
        demand.workloads,
        baseline_projections,
        strict=True,
    ):
        reserved_projection = _simulate_reserved_projection(
            simulator=simulator,
            workload=workload,
            candidate=candidate,
            remaining_capacity=remaining_capacity,
        )
        selected_cost = reserved_projection.selected_cost if reserved_projection else None
        if selected_cost is not None and remaining_capacity.get(selected_cost.provider_id, 0) > 0:
            remaining_capacity[selected_cost.provider_id] -= 1
            covered += 1
            marginal_cost += selected_cost.upper_bound_usd
            _increment_count(selected_counts, selected_cost.provider_id)
            continue

        baseline_cost = baseline_projection.selected_cost
        if baseline_cost is None:
            unmet += 1
            continue

        overflow += 1
        marginal_cost += baseline_projection.reserved_usd
        _increment_count(selected_counts, baseline_cost.provider_id)

    marginal = _usd(marginal_cost, "marginal_cost_usd")
    total = _usd(fixed_cost + marginal, "total_cost_usd")
    budget_after, runway_after = _budget_metadata(
        total_cost_usd=total,
        burn_rate_forecast=burn_rate_forecast,
    )
    return PlanEvaluation(
        plan=candidate,
        fixed_cost_usd=fixed_cost,
        marginal_cost_usd=marginal,
        total_cost_usd=total,
        projected_savings_usd=_signed_usd(
            baseline_total_usd - total,
            "projected_savings_usd",
        ),
        covered_workloads=covered,
        on_demand_overflow_workloads=overflow,
        unmet_workloads=unmet,
        meets_demand=unmet == 0,
        selected_provider_counts=selected_counts,
        budget_after_plan_usd=budget_after,
        runway_days_after_plan=runway_after,
    )


def _simulate_reserved_projection(
    *,
    simulator: WhatIfSimulator,
    workload: WhatIfWorkload,
    candidate: ReservationCandidate,
    remaining_capacity: Mapping[str, int],
) -> WhatIfProjection | None:
    active_overrides = _active_price_overrides(candidate, remaining_capacity)
    if not active_overrides:
        return None
    return simulator.simulate(
        workload.request,
        payload=workload.payload,
        price_overrides=active_overrides,
    )


def _active_price_overrides(
    candidate: ReservationCandidate,
    remaining_capacity: Mapping[str, int],
) -> Mapping[str, object]:
    return {
        provider_id: override
        for provider_id, override in candidate.price_overrides.items()
        if remaining_capacity.get(provider_id, 0) > 0
    }


def _selected_provider_counts(projections: Sequence[WhatIfProjection]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for projection in projections:
        selected_cost = projection.selected_cost
        if selected_cost is not None:
            _increment_count(counts, selected_cost.provider_id)
    return counts


def _increment_count(counts: dict[str, int], provider_id: str) -> None:
    counts[provider_id] = counts.get(provider_id, 0) + 1


def _budget_metadata(
    *,
    total_cost_usd: Decimal,
    burn_rate_forecast: BurnRateForecast | None,
) -> tuple[Decimal | None, Decimal | None]:
    if burn_rate_forecast is None:
        return None, None
    budget_after = _signed_usd(
        burn_rate_forecast.remaining_budget_usd - total_cost_usd,
        "budget_after_plan_usd",
    )
    if budget_after <= 0 or burn_rate_forecast.burn_rate_usd_per_day <= 0:
        return budget_after, None
    runway = _non_negative_decimal(
        budget_after / burn_rate_forecast.burn_rate_usd_per_day,
        "runway_days_after_plan",
    )
    return budget_after, runway


def _recommendation_sort_key(evaluation: PlanEvaluation) -> tuple[Decimal, int, str]:
    on_demand_priority = 0 if evaluation.plan_id == "on_demand" else 1
    return evaluation.total_cost_usd, on_demand_priority, evaluation.plan_id


def _burn_rate_to_dict(forecast: BurnRateForecast | None) -> dict[str, str | None] | None:
    if forecast is None:
        return None
    return {
        "burn_rate_usd_per_day": _decimal_to_str(forecast.burn_rate_usd_per_day),
        "projected_exhaustion": (
            forecast.projected_exhaustion.isoformat()
            if forecast.projected_exhaustion is not None
            else None
        ),
        "trend": forecast.trend,
        "confidence": _decimal_to_str(forecast.confidence),
        "budget_usd": _decimal_to_str(forecast.budget_usd),
        "remaining_budget_usd": _decimal_to_str(forecast.remaining_budget_usd),
        "runway_days": _optional_decimal_to_str(forecast.runway_days),
    }


def _validate_non_negative_int(value: int, name: str) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _usd(value: object, name: str) -> Decimal:
    decimal_value = _non_negative_decimal(value, name)
    return _quantize(decimal_value, name)


def _signed_usd(value: object, name: str) -> Decimal:
    return _quantize(_decimal(value, name), name)


def _non_negative_decimal(value: object, name: str) -> Decimal:
    decimal_value = _decimal(value, name)
    if decimal_value < 0:
        raise ValueError(f"{name} must be non-negative")
    return decimal_value


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


def _quantize(value: Decimal, name: str) -> Decimal:
    try:
        return value.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"{name} is out of representable range: {value}") from exc


def _freeze_mapping(mapping: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({str(key): _freeze_value(value) for key, value in mapping.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(cast(Mapping[str, object], value))
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _decimal_to_str(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _decimal_to_str(value)


__all__ = [
    "DemandForecast",
    "PlanEvaluation",
    "RecommendationAction",
    "ReservationCandidate",
    "ReservationLine",
    "ReservationRecommendation",
    "ReservationRecommender",
    "recommend_reservations",
]
