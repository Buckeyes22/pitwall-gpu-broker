"""Hermetic unit tests for the recommendations engine.

Covers all four signal planes (drift, burn-rate, reservations, scorecards),
validation, sorting, determinism, and property-based invariants.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.finops.burn_rate import BurnRateForecast
from pitwall.finops.reservations import (
    DemandForecast,
    PlanEvaluation,
    ReservationCandidate,
    ReservationRecommendation,
)
from pitwall.providers.drift import DriftFinding, DriftSeverity
from pitwall.recommendations.engine import (
    Recommendation,
    RecommendationCategory,
    RecommendationEngine,
    ScorecardMetric,
)

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine(
    *,
    runway_critical: str = "3",
    runway_warning: str = "7",
    scorecard_threshold: str = "0.15",
) -> RecommendationEngine:
    return RecommendationEngine(
        runway_critical_days=Decimal(runway_critical),
        runway_warning_days=Decimal(runway_warning),
        scorecard_threshold=Decimal(scorecard_threshold),
    )


def _drift(
    *,
    field: str = "enabled",
    severity: DriftSeverity = DriftSeverity.HIGH,
    expected: Any = True,
    observed: Any = False,
    message: str = "",
) -> DriftFinding:
    return DriftFinding(
        provider_id="prov_test",
        field=field,
        expected=expected,
        observed=observed,
        severity=severity,
        message=message,
    )


def _burn_rate(
    *,
    runway_days: Decimal | None = None,
    trend: str = "stable",
    confidence: str = "1",
    remaining: str = "100",
    burn_rate: str = "10",
) -> BurnRateForecast:
    return BurnRateForecast(
        burn_rate_usd_per_day=Decimal(burn_rate),
        projected_exhaustion=None,
        trend=cast(Any, trend),
        confidence=Decimal(confidence),
        budget_usd=Decimal("1000"),
        remaining_budget_usd=Decimal(remaining),
        runway_days=runway_days,
    )


def _reservation(
    *,
    action: str = "on_demand",
    savings: str = "0",
) -> ReservationRecommendation:
    baseline = PlanEvaluation(
        plan=ReservationCandidate(plan_id="on_demand"),
        fixed_cost_usd=Decimal("0"),
        marginal_cost_usd=Decimal("10"),
        total_cost_usd=Decimal("10"),
        projected_savings_usd=Decimal(savings),
        covered_workloads=0,
        on_demand_overflow_workloads=0,
        unmet_workloads=0,
        meets_demand=True,
    )
    return ReservationRecommendation(
        demand=DemandForecast(name="test", workloads=()),
        baseline=baseline,
        evaluations=(baseline,),
        recommended=baseline,
        action=cast(Any, action),
    )


def _scorecard(
    *,
    score: str = "0.5",
    benchmark: str = "0.8",
    dimension: str = "cost",
) -> ScorecardMetric:
    return ScorecardMetric(
        capability_id="cap_test",
        provider_id="prov_test",
        dimension=dimension,
        score=Decimal(score),
        benchmark=Decimal(benchmark),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestScorecardMetricValidation:
    def test_empty_capability_id_raises(self) -> None:
        with pytest.raises(ValueError, match="capability_id must be non-empty"):
            ScorecardMetric(
                capability_id="",
                provider_id="p",
                dimension="cost",
                score=Decimal("0.5"),
                benchmark=Decimal("0.5"),
            )

    def test_empty_provider_id_raises(self) -> None:
        with pytest.raises(ValueError, match="provider_id must be non-empty"):
            ScorecardMetric(
                capability_id="c",
                provider_id="",
                dimension="cost",
                score=Decimal("0.5"),
                benchmark=Decimal("0.5"),
            )

    def test_empty_dimension_raises(self) -> None:
        with pytest.raises(ValueError, match="dimension must be non-empty"):
            ScorecardMetric(
                capability_id="c",
                provider_id="p",
                dimension="",
                score=Decimal("0.5"),
                benchmark=Decimal("0.5"),
            )

    def test_score_clamped_to_0_1(self) -> None:
        m = ScorecardMetric(
            capability_id="c",
            provider_id="p",
            dimension="cost",
            score=Decimal("1.5"),
            benchmark=Decimal("-0.3"),
        )
        assert m.score == Decimal("1")
        assert m.benchmark == Decimal("0")


class TestRecommendationValidation:
    def test_empty_action_raises(self) -> None:
        with pytest.raises(ValueError, match="action must be non-empty"):
            Recommendation(
                action="",
                category=RecommendationCategory.DRIFT,
                target_provider_id=None,
                target_capability_id=None,
                rationale="r",
                estimated_impact_usd=Decimal("0"),
                confidence=Decimal("0.5"),
                source_signals=(),
                priority=1,
            )

    def test_empty_rationale_raises(self) -> None:
        with pytest.raises(ValueError, match="rationale must be non-empty"):
            Recommendation(
                action="a",
                category=RecommendationCategory.DRIFT,
                target_provider_id=None,
                target_capability_id=None,
                rationale="",
                estimated_impact_usd=Decimal("0"),
                confidence=Decimal("0.5"),
                source_signals=(),
                priority=1,
            )

    def test_priority_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="priority must be >= 1"):
            Recommendation(
                action="a",
                category=RecommendationCategory.DRIFT,
                target_provider_id=None,
                target_capability_id=None,
                rationale="r",
                estimated_impact_usd=Decimal("0"),
                confidence=Decimal("0.5"),
                source_signals=(),
                priority=0,
            )

    def test_confidence_clamped(self) -> None:
        rec = Recommendation(
            action="a",
            category=RecommendationCategory.DRIFT,
            target_provider_id=None,
            target_capability_id=None,
            rationale="r",
            estimated_impact_usd=Decimal("0"),
            confidence=Decimal("1.5"),
            source_signals=(),
            priority=1,
        )
        assert rec.confidence == Decimal("1")


class TestEngineValidation:
    def test_non_positive_runway_critical_raises(self) -> None:
        with pytest.raises(ValueError, match="runway_critical_days must be positive"):
            _engine(runway_critical="0")

    def test_non_positive_runway_warning_raises(self) -> None:
        with pytest.raises(ValueError, match="runway_warning_days must be positive"):
            _engine(runway_warning="0")


# ---------------------------------------------------------------------------
# Drift signals
# ---------------------------------------------------------------------------


class TestDriftRecommendations:
    def test_provider_id_mismatch_is_priority_1(self) -> None:
        recs = _engine().recommend(
            drift_findings=[
                _drift(
                    field="provider_id",
                    severity=DriftSeverity.CRITICAL,
                    expected="prov_a",
                    observed="prov_b",
                )
            ]
        )
        assert len(recs) == 1
        assert recs[0].priority == 1
        assert recs[0].action == "investigate_provider_id_mismatch"
        assert recs[0].category == RecommendationCategory.DRIFT

    def test_disabled_but_running(self) -> None:
        recs = _engine().recommend(
            drift_findings=[
                _drift(
                    field="enabled",
                    severity=DriftSeverity.HIGH,
                    expected=False,
                    observed=True,
                )
            ]
        )
        assert len(recs) == 1
        assert recs[0].priority == 5
        assert recs[0].action == "disable_or_investigate_running_provider"

    def test_enabled_but_terminated(self) -> None:
        recs = _engine().recommend(
            drift_findings=[
                _drift(
                    field="enabled",
                    severity=DriftSeverity.MEDIUM,
                    expected=True,
                    observed=False,
                )
            ]
        )
        assert len(recs) == 1
        assert recs[0].priority == 10
        assert recs[0].action == "reconcile_provider_enablement"

    def test_health_mismatch(self) -> None:
        recs = _engine().recommend(
            drift_findings=[
                _drift(
                    field="health_status",
                    severity=DriftSeverity.HIGH,
                    expected="healthy",
                    observed="unhealthy",
                )
            ]
        )
        assert len(recs) == 1
        assert recs[0].action == "investigate_provider_health"
        assert recs[0].target_provider_id == "prov_test"

    def test_price_drift(self) -> None:
        recs = _engine().recommend(
            drift_findings=[
                _drift(
                    field="price_per_second",
                    severity=DriftSeverity.MEDIUM,
                    expected=Decimal("0.001"),
                    observed=Decimal("0.002"),
                )
            ]
        )
        assert len(recs) == 1
        assert recs[0].action == "update_provider_pricing_or_switch"
        assert recs[0].priority == 10

    def test_availability_drift(self) -> None:
        recs = _engine().recommend(
            drift_findings=[
                _drift(
                    field="availability",
                    severity=DriftSeverity.HIGH,
                    expected=True,
                    observed=False,
                    message="Provider is enabled but observed as unavailable",
                )
            ]
        )
        assert len(recs) == 1
        assert recs[0].action == "check_provider_availability"
        assert "unavailable" in recs[0].rationale

    def test_unknown_drift_field_ignored(self) -> None:
        recs = _engine().recommend(
            drift_findings=[_drift(field="unknown_field", severity=DriftSeverity.LOW)]
        )
        assert len(recs) == 0

    def test_multiple_drift_sorted_by_priority(self) -> None:
        recs = _engine().recommend(
            drift_findings=[
                _drift(field="availability", severity=DriftSeverity.MEDIUM),
                _drift(field="health_status", severity=DriftSeverity.HIGH),
                _drift(field="price_per_second", severity=DriftSeverity.LOW),
            ]
        )
        priorities = [r.priority for r in recs]
        assert priorities == [5, 10, 15]


# ---------------------------------------------------------------------------
# Burn-rate signals
# ---------------------------------------------------------------------------


class TestBurnRateRecommendations:
    def test_critical_runway_priority_2(self) -> None:
        recs = _engine().recommend(burn_rate=_burn_rate(runway_days=Decimal("2"), remaining="50"))
        assert len(recs) == 1
        assert recs[0].priority == 2
        assert recs[0].action == "reduce_spend_or_increase_budget"
        assert recs[0].estimated_impact_usd == Decimal("50")

    def test_warning_runway_priority_6(self) -> None:
        recs = _engine().recommend(burn_rate=_burn_rate(runway_days=Decimal("5"), remaining="200"))
        assert len(recs) == 1
        assert recs[0].priority == 6
        assert recs[0].action == "review_spend_trend"

    def test_increasing_trend_priority_8(self) -> None:
        recs = _engine().recommend(burn_rate=_burn_rate(trend="increasing", confidence="0.8"))
        assert len(recs) == 1
        assert recs[0].priority == 8
        assert recs[0].action == "investigate_spend_acceleration"
        assert recs[0].confidence == Decimal("0.800000")

    def test_critical_and_increasing_gives_two_recs(self) -> None:
        recs = _engine().recommend(
            burn_rate=_burn_rate(runway_days=Decimal("2"), trend="increasing", remaining="50")
        )
        assert len(recs) == 2
        assert recs[0].priority == 2
        assert recs[1].priority == 8

    def test_no_recommendation_when_runway_long(self) -> None:
        recs = _engine().recommend(burn_rate=_burn_rate(runway_days=Decimal("30")))
        assert len(recs) == 0

    def test_no_recommendation_when_none(self) -> None:
        recs = _engine().recommend(burn_rate=None)
        assert len(recs) == 0


# ---------------------------------------------------------------------------
# Reservation signals
# ---------------------------------------------------------------------------


class TestReservationRecommendations:
    def test_reserve_action(self) -> None:
        recs = _engine().recommend(reservation=_reservation(action="reserve", savings="25"))
        assert len(recs) == 1
        assert recs[0].action == "reserve_capacity"
        assert recs[0].estimated_impact_usd == Decimal("25")
        assert recs[0].priority == 4

    def test_blocked_action(self) -> None:
        recs = _engine().recommend(reservation=_reservation(action="blocked"))
        assert len(recs) == 1
        assert recs[0].action == "review_provider_pool"
        assert recs[0].priority == 3

    def test_on_demand_action_ignored(self) -> None:
        recs = _engine().recommend(reservation=_reservation(action="on_demand"))
        assert len(recs) == 0

    def test_none_ignored(self) -> None:
        recs = _engine().recommend(reservation=None)
        assert len(recs) == 0


# ---------------------------------------------------------------------------
# Scorecard signals
# ---------------------------------------------------------------------------


class TestScorecardRecommendations:
    def test_below_threshold_generates_recommendation(self) -> None:
        recs = _engine().recommend(scorecards=[_scorecard(score="0.5", benchmark="0.8")])
        assert len(recs) == 1
        assert recs[0].action == "switch_to_better_provider"
        assert recs[0].category == RecommendationCategory.SCORECARD
        assert recs[0].target_provider_id == "prov_test"
        assert recs[0].target_capability_id == "cap_test"
        assert recs[0].priority == 12

    def test_exactly_at_threshold_ignored(self) -> None:
        recs = _engine().recommend(scorecards=[_scorecard(score="0.7", benchmark="0.85")])
        assert len(recs) == 0

    def test_above_threshold_ignored(self) -> None:
        recs = _engine().recommend(scorecards=[_scorecard(score="0.9", benchmark="0.8")])
        assert len(recs) == 0

    def test_custom_threshold(self) -> None:
        engine = _engine(scorecard_threshold="0.05")
        recs = engine.recommend(scorecards=[_scorecard(score="0.7", benchmark="0.76")])
        assert len(recs) == 1


# ---------------------------------------------------------------------------
# Cross-signal integration & sorting
# ---------------------------------------------------------------------------


class TestIntegrationAndSorting:
    def test_all_signals_ranked_correctly(self) -> None:
        engine = _engine()
        recs = engine.recommend(
            drift_findings=[_drift(field="health_status", severity=DriftSeverity.HIGH)],
            burn_rate=_burn_rate(runway_days=Decimal("2")),
            reservation=_reservation(action="blocked"),
            scorecards=[_scorecard(score="0.5", benchmark="0.8")],
        )
        assert len(recs) == 4
        actions = [r.action for r in recs]
        assert actions == [
            "reduce_spend_or_increase_budget",  # priority 2
            "review_provider_pool",  # priority 3
            "investigate_provider_health",  # priority 5
            "switch_to_better_provider",  # priority 12
        ]

    def test_determinism(self) -> None:
        engine = _engine()
        first = engine.recommend(
            drift_findings=[
                _drift(field="price_per_second", severity=DriftSeverity.MEDIUM),
                _drift(field="availability", severity=DriftSeverity.HIGH),
            ],
            burn_rate=_burn_rate(runway_days=Decimal("5")),
            reservation=_reservation(action="reserve", savings="10"),
            scorecards=[_scorecard(score="0.4", benchmark="0.7")],
        )
        second = engine.recommend(
            drift_findings=[
                _drift(field="price_per_second", severity=DriftSeverity.MEDIUM),
                _drift(field="availability", severity=DriftSeverity.HIGH),
            ],
            burn_rate=_burn_rate(runway_days=Decimal("5")),
            reservation=_reservation(action="reserve", savings="10"),
            scorecards=[_scorecard(score="0.4", benchmark="0.7")],
        )
        assert first == second

    def test_to_dict_roundtrips(self) -> None:
        engine = _engine()
        recs = engine.recommend(
            drift_findings=[_drift(field="health_status", severity=DriftSeverity.HIGH)]
        )
        d = recs[0].to_dict()
        assert d["action"] == "investigate_provider_health"
        assert d["category"] == "drift"
        assert d["priority"] == 5
        assert "source_signals" in d

    def test_empty_signals_returns_empty(self) -> None:
        assert _engine().recommend() == []


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(
    score=st.decimals(allow_nan=False, allow_infinity=False, places=2, min_value=0, max_value=1),
    benchmark=st.decimals(
        allow_nan=False, allow_infinity=False, places=2, min_value=0, max_value=1
    ),
    threshold=st.decimals(
        allow_nan=False,
        allow_infinity=False,
        places=2,
        min_value=Decimal("0.01"),
        max_value=Decimal("0.5"),
    ),
)
def test_scorecard_recommendation_monotonic_in_gap(
    score: Decimal, benchmark: Decimal, threshold: Decimal
) -> None:
    """A larger gap always produces a recommendation when threshold is low enough."""
    engine = _engine(scorecard_threshold=str(threshold))
    recs = engine.recommend(
        scorecards=[
            ScorecardMetric(
                capability_id="cap",
                provider_id="prov",
                dimension="cost",
                score=score,
                benchmark=benchmark,
            )
        ]
    )
    gap = benchmark - score
    if gap > threshold:
        assert len(recs) == 1
        assert recs[0].action == "switch_to_better_provider"
    else:
        assert len(recs) == 0


@given(
    runway_days=st.one_of(
        st.none(),
        st.decimals(allow_nan=False, allow_infinity=False, places=2, min_value=0, max_value=100),
    ),
    trend=st.sampled_from(["increasing", "decreasing", "stable", "insufficient_data"]),
)
def test_burn_rate_never_crashes(runway_days: Decimal | None, trend: str) -> None:
    """The engine must never raise on arbitrary burn-rate inputs."""
    engine = _engine()
    burn = _burn_rate(runway_days=runway_days, trend=trend)
    try:
        recs = engine.recommend(burn_rate=burn)
        assert isinstance(recs, list)
        for rec in recs:
            assert rec.priority >= 1
    except Exception:  # reason: any engine raise on valid input is the failure under test
        pytest.fail("Engine raised on valid burn-rate input")
