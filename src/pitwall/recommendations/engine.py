"""Recommendations engine — aggregate signals into ranked, actionable operator guidance.

The engine is deterministic and read-only given input snapshots.  It never mutates
state or auto-applies changes.  Output is a sorted list of :class:`Recommendation`
objects ordered by priority (most urgent first).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from pitwall.finops.burn_rate import BurnRateForecast
from pitwall.finops.reservations import ReservationRecommendation
from pitwall.providers.drift import DriftFinding, DriftSeverity

_USD_QUANTUM = Decimal("0.000001")

# ---------------------------------------------------------------------------
# Scorecard contract (lightweight; mirrors planned observability/scorecards.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScorecardMetric:
    """One cost×latency×quality metric for a (capability, provider) pair.

    This is a minimal contract so the recommendations engine can consume
    scorecard signals before the full observability/scorecards module lands.
    """

    capability_id: str
    provider_id: str
    dimension: str
    score: Decimal
    benchmark: Decimal
    message: str = ""

    def __post_init__(self) -> None:
        if not self.capability_id:
            raise ValueError("capability_id must be non-empty")
        if not self.provider_id:
            raise ValueError("provider_id must be non-empty")
        if not self.dimension:
            raise ValueError("dimension must be non-empty")
        object.__setattr__(self, "score", _quantize(_clamp_01(self.score), "score"))
        object.__setattr__(self, "benchmark", _quantize(_clamp_01(self.benchmark), "benchmark"))


# ---------------------------------------------------------------------------
# Recommendation types
# ---------------------------------------------------------------------------


class RecommendationCategory(StrEnum):
    """High-level bucket for a recommendation."""

    DRIFT = "drift"
    BUDGET = "budget"
    CAPACITY = "capacity"
    SCORECARD = "scorecard"


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One actionable, prioritized operator recommendation."""

    action: str
    category: RecommendationCategory
    target_provider_id: str | None
    target_capability_id: str | None
    rationale: str
    estimated_impact_usd: Decimal
    confidence: Decimal
    source_signals: tuple[str, ...]
    priority: int

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("action must be non-empty")
        if not self.rationale:
            raise ValueError("rationale must be non-empty")
        object.__setattr__(
            self,
            "estimated_impact_usd",
            _signed_usd(self.estimated_impact_usd, "estimated_impact_usd"),
        )
        object.__setattr__(self, "confidence", _quantize(_clamp_01(self.confidence), "confidence"))
        if self.priority < 1:
            raise ValueError("priority must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "category": self.category.value,
            "target_provider_id": self.target_provider_id,
            "target_capability_id": self.target_capability_id,
            "rationale": self.rationale,
            "estimated_impact_usd": _decimal_to_str(self.estimated_impact_usd),
            "confidence": _decimal_to_str(self.confidence),
            "source_signals": list(self.source_signals),
            "priority": self.priority,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecommendationEngine:
    """Pure, deterministic recommender aggregating four signal planes."""

    runway_critical_days: Decimal = Decimal("3")
    runway_warning_days: Decimal = Decimal("7")
    scorecard_threshold: Decimal = Decimal("0.15")

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "runway_critical_days",
            _positive_decimal(self.runway_critical_days, "runway_critical_days"),
        )
        object.__setattr__(
            self,
            "runway_warning_days",
            _positive_decimal(self.runway_warning_days, "runway_warning_days"),
        )
        object.__setattr__(
            self,
            "scorecard_threshold",
            _quantize(_clamp_01(self.scorecard_threshold), "scorecard_threshold"),
        )

    def recommend(
        self,
        *,
        scorecards: Sequence[ScorecardMetric] = (),
        drift_findings: Sequence[DriftFinding] = (),
        burn_rate: BurnRateForecast | None = None,
        reservation: ReservationRecommendation | None = None,
    ) -> list[Recommendation]:
        """Return ranked recommendations from the supplied signal snapshots.

        Results are sorted by *priority* ascending (most urgent first) and are
        deterministic given identical inputs.
        """
        recommendations: list[Recommendation] = []
        recommendations.extend(_from_drift(drift_findings))
        recommendations.extend(_from_burn_rate(burn_rate, self))
        recommendations.extend(_from_reservation(reservation))
        recommendations.extend(_from_scorecards(scorecards, self))
        recommendations.sort(key=_recommendation_sort_key)
        return recommendations


# ---------------------------------------------------------------------------
# Signal → recommendation mappers
# ---------------------------------------------------------------------------


def _from_drift(findings: Sequence[DriftFinding]) -> list[Recommendation]:
    recommendations: list[Recommendation] = []
    for finding in findings:
        rec = _drift_finding_to_recommendation(finding)
        if rec is not None:
            recommendations.append(rec)
    return recommendations


def _drift_finding_to_recommendation(finding: DriftFinding) -> Recommendation | None:
    priority = _drift_priority(finding.severity)
    if finding.field == "provider_id":
        return Recommendation(
            action="investigate_provider_id_mismatch",
            category=RecommendationCategory.DRIFT,
            target_provider_id=finding.provider_id,
            target_capability_id=None,
            rationale=(
                f"Observed provider id {finding.observed!r} does not match "
                f"expected {finding.expected!r}"
            ),
            estimated_impact_usd=Decimal("0"),
            confidence=Decimal("1"),
            source_signals=(f"drift:{finding.field}",),
            priority=priority,
        )
    if finding.field == "enabled":
        if finding.expected is False and finding.observed is True:
            return Recommendation(
                action="disable_or_investigate_running_provider",
                category=RecommendationCategory.DRIFT,
                target_provider_id=finding.provider_id,
                target_capability_id=None,
                rationale=(
                    f"Provider {finding.provider_id} is disabled but a running "
                    f"resource was observed"
                ),
                estimated_impact_usd=Decimal("0"),
                confidence=Decimal("0.9"),
                source_signals=(f"drift:{finding.field}",),
                priority=priority,
            )
        return Recommendation(
            action="reconcile_provider_enablement",
            category=RecommendationCategory.DRIFT,
            target_provider_id=finding.provider_id,
            target_capability_id=None,
            rationale=(
                f"Provider {finding.provider_id} is enabled but observed resource is terminated"
            ),
            estimated_impact_usd=Decimal("0"),
            confidence=Decimal("0.8"),
            source_signals=(f"drift:{finding.field}",),
            priority=priority,
        )
    if finding.field == "health_status":
        return Recommendation(
            action="investigate_provider_health",
            category=RecommendationCategory.DRIFT,
            target_provider_id=finding.provider_id,
            target_capability_id=None,
            rationale=(f"Expected health {finding.expected!r}, observed {finding.observed!r}"),
            estimated_impact_usd=Decimal("0"),
            confidence=Decimal("0.85"),
            source_signals=(f"drift:{finding.field}",),
            priority=priority,
        )
    if finding.field == "price_per_second":
        return Recommendation(
            action="update_provider_pricing_or_switch",
            category=RecommendationCategory.DRIFT,
            target_provider_id=finding.provider_id,
            target_capability_id=None,
            rationale=(
                f"Observed price {finding.observed} differs from configured "
                f"price {finding.expected}"
            ),
            estimated_impact_usd=Decimal("0"),
            confidence=Decimal("0.75"),
            source_signals=(f"drift:{finding.field}",),
            priority=priority,
        )
    if finding.field == "availability":
        rationale = finding.message or (
            f"Provider {finding.provider_id} availability drift detected"
        )
        return Recommendation(
            action="check_provider_availability",
            category=RecommendationCategory.DRIFT,
            target_provider_id=finding.provider_id,
            target_capability_id=None,
            rationale=rationale,
            estimated_impact_usd=Decimal("0"),
            confidence=Decimal("0.8"),
            source_signals=(f"drift:{finding.field}",),
            priority=priority,
        )
    return None


def _drift_priority(severity: DriftSeverity) -> int:
    mapping = {
        DriftSeverity.CRITICAL: 1,
        DriftSeverity.HIGH: 5,
        DriftSeverity.MEDIUM: 10,
        DriftSeverity.LOW: 15,
        DriftSeverity.INFO: 20,
    }
    return mapping.get(severity, 20)


def _from_burn_rate(
    burn_rate: BurnRateForecast | None,
    engine: RecommendationEngine,
) -> list[Recommendation]:
    if burn_rate is None:
        return []
    recommendations: list[Recommendation] = []
    runway = burn_rate.runway_days
    if runway is not None and runway <= engine.runway_critical_days:
        recommendations.append(
            Recommendation(
                action="reduce_spend_or_increase_budget",
                category=RecommendationCategory.BUDGET,
                target_provider_id=None,
                target_capability_id=None,
                rationale=(
                    f"Budget runway is critically short: {runway} days "
                    f"(burn rate {burn_rate.burn_rate_usd_per_day} USD/day)"
                ),
                estimated_impact_usd=burn_rate.remaining_budget_usd,
                confidence=burn_rate.confidence,
                source_signals=("burn_rate:runway",),
                priority=2,
            )
        )
    elif runway is not None and runway <= engine.runway_warning_days:
        recommendations.append(
            Recommendation(
                action="review_spend_trend",
                category=RecommendationCategory.BUDGET,
                target_provider_id=None,
                target_capability_id=None,
                rationale=(
                    f"Budget runway is {runway} days; monitor closely (trend: {burn_rate.trend})"
                ),
                estimated_impact_usd=burn_rate.remaining_budget_usd,
                confidence=burn_rate.confidence,
                source_signals=("burn_rate:runway",),
                priority=6,
            )
        )
    if burn_rate.trend == "increasing":
        recommendations.append(
            Recommendation(
                action="investigate_spend_acceleration",
                category=RecommendationCategory.BUDGET,
                target_provider_id=None,
                target_capability_id=None,
                rationale=(
                    f"Spend is accelerating (burn rate {burn_rate.burn_rate_usd_per_day} USD/day)"
                ),
                estimated_impact_usd=burn_rate.remaining_budget_usd,
                confidence=burn_rate.confidence,
                source_signals=("burn_rate:trend",),
                priority=8,
            )
        )
    return recommendations


def _from_reservation(
    reservation: ReservationRecommendation | None,
) -> list[Recommendation]:
    if reservation is None:
        return []
    recommendations: list[Recommendation] = []
    if reservation.action == "reserve":
        savings = reservation.projected_savings_usd
        recommendations.append(
            Recommendation(
                action="reserve_capacity",
                category=RecommendationCategory.CAPACITY,
                target_provider_id=None,
                target_capability_id=None,
                rationale=(
                    f"Reservation plan {reservation.recommended.plan_id!r} "
                    f"saves {savings} USD vs on-demand"
                ),
                estimated_impact_usd=savings,
                confidence=Decimal("0.9"),
                source_signals=("reservation:recommendation",),
                priority=4,
            )
        )
    elif reservation.action == "blocked":
        recommendations.append(
            Recommendation(
                action="review_provider_pool",
                category=RecommendationCategory.CAPACITY,
                target_provider_id=None,
                target_capability_id=None,
                rationale=(
                    "No viable reservation plan meets demand; review provider pool and pricing"
                ),
                estimated_impact_usd=Decimal("0"),
                confidence=Decimal("0.7"),
                source_signals=("reservation:blocked",),
                priority=3,
            )
        )
    return recommendations


def _from_scorecards(
    scorecards: Sequence[ScorecardMetric],
    engine: RecommendationEngine,
) -> list[Recommendation]:
    recommendations: list[Recommendation] = []
    for metric in scorecards:
        gap = metric.benchmark - metric.score
        if gap > engine.scorecard_threshold:
            recommendations.append(
                Recommendation(
                    action="switch_to_better_provider",
                    category=RecommendationCategory.SCORECARD,
                    target_provider_id=metric.provider_id,
                    target_capability_id=metric.capability_id,
                    rationale=(
                        f"{metric.dimension} score {metric.score} is "
                        f"{gap} below benchmark {metric.benchmark}"
                        + (f"; {metric.message}" if metric.message else "")
                    ),
                    estimated_impact_usd=Decimal("0"),
                    confidence=_quantize(Decimal(min(1.0, float(gap) * 2)), "confidence"),
                    source_signals=(f"scorecard:{metric.dimension}",),
                    priority=12,
                )
            )
    return recommendations


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def _recommendation_sort_key(rec: Recommendation) -> tuple[int, Decimal, str]:
    return rec.priority, Decimal("1") - rec.confidence, rec.action


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _quantize(value: Decimal, name: str) -> Decimal:
    try:
        return value.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    except Exception as exc:  # reason: convert any quantize failure to a named ValueError
        raise ValueError(f"{name} is out of representable range: {value}") from exc


def _clamp_01(value: Decimal) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception as exc:  # reason: convert any Decimal parse failure to a named ValueError
        raise ValueError("value must be a decimal between 0 and 1") from exc
    if not d.is_finite():
        raise ValueError("value must be finite")
    if d < 0:
        return Decimal("0")
    if d > 1:
        return Decimal("1")
    return d


def _positive_decimal(value: object, name: str) -> Decimal:
    d = _decimal(value, name)
    if d <= 0:
        raise ValueError(f"{name} must be positive")
    return d


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal value")
    try:
        d = Decimal(str(value))
    except Exception as exc:  # reason: convert any Decimal parse failure to a named ValueError
        raise ValueError(f"{name} must be a decimal value") from exc
    if not d.is_finite():
        raise ValueError(f"{name} must be finite")
    return d


def _signed_usd(value: object, name: str) -> Decimal:
    return _quantize(_decimal(value, name), name)


def _decimal_to_str(value: Decimal) -> str:
    return format(value, "f")


__all__ = [
    "Recommendation",
    "RecommendationCategory",
    "RecommendationEngine",
    "ScorecardMetric",
]
