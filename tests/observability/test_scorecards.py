"""Hermetic unit tests for the scorecard builder."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from pitwall.observability.scorecards import (
    ScorecardBuilder,
    ScorecardObservation,
    observations_from_workloads,
)

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)


def _obs(
    provider_id: str = "p1",
    capability_id: str = "c1",
    cost_usd: str | Decimal = "1.00",
    latency_ms: float = 100.0,
    quality: float = 1.0,
    observed_at: dt.datetime | None = _NOW,
) -> ScorecardObservation:
    return ScorecardObservation(
        provider_id=provider_id,
        capability_id=capability_id,
        cost_usd=Decimal(str(cost_usd)),
        latency_ms=latency_ms,
        quality=quality,
        observed_at=observed_at,
    )


class TestScorecardBuilderBasics:
    def test_empty_observations_returns_empty(self) -> None:
        builder = ScorecardBuilder()
        assert builder.build([]) == ()

    def test_single_observation_all_scores_one(self) -> None:
        builder = ScorecardBuilder()
        result = builder.build([_obs(cost_usd="10", latency_ms=100, quality=0.5)])
        assert len(result) == 1
        card = result[0]
        assert card.rank == 1
        assert card.cost_normalized == 1.0
        assert card.latency_normalized == 1.0
        assert card.quality_normalized == 1.0
        assert card.composite_score == 1.0

    def test_two_providers_ranked_by_composite(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(provider_id="cheap", cost_usd="1", latency_ms=50, quality=1.0),
            _obs(provider_id="expensive", cost_usd="10", latency_ms=200, quality=0.5),
        ]
        result = builder.build(obs)
        assert len(result) == 2
        cheap = result[0]
        expensive = result[1]
        assert cheap.provider_id == "cheap"
        assert cheap.rank == 1
        assert cheap.composite_score > expensive.composite_score
        assert expensive.rank == 2

    def test_cost_normalization_lower_is_better(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(provider_id="a", cost_usd="1"),
            _obs(provider_id="b", cost_usd="10"),
        ]
        result = builder.build(obs)
        a = result[0] if result[0].provider_id == "a" else result[1]
        b = result[1] if result[1].provider_id == "b" else result[0]
        assert a.cost_normalized == 1.0
        assert b.cost_normalized == 0.0

    def test_latency_normalization_lower_is_better(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(provider_id="a", latency_ms=50),
            _obs(provider_id="b", latency_ms=150),
        ]
        result = builder.build(obs)
        a = next(r for r in result if r.provider_id == "a")
        b = next(r for r in result if r.provider_id == "b")
        assert a.latency_normalized == 1.0
        assert b.latency_normalized == 0.0

    def test_quality_normalization_higher_is_better(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(provider_id="a", quality=0.2),
            _obs(provider_id="b", quality=0.8),
        ]
        result = builder.build(obs)
        a = next(r for r in result if r.provider_id == "a")
        b = next(r for r in result if r.provider_id == "b")
        assert a.quality_normalized == 0.0
        assert b.quality_normalized == 1.0

    def test_grouped_by_provider_and_capability(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(provider_id="p1", capability_id="c1", cost_usd="1"),
            _obs(provider_id="p1", capability_id="c2", cost_usd="2"),
            _obs(provider_id="p2", capability_id="c1", cost_usd="3"),
        ]
        result = builder.build(obs)
        assert len(result) == 3
        ids = {(r.provider_id, r.capability_id) for r in result}
        assert ids == {("p1", "c1"), ("p1", "c2"), ("p2", "c1")}

    def test_observation_count_reflects_group_size(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(provider_id="p1", capability_id="c1", cost_usd="1"),
            _obs(provider_id="p1", capability_id="c1", cost_usd="2"),
            _obs(provider_id="p1", capability_id="c1", cost_usd="3"),
        ]
        result = builder.build(obs)
        assert len(result) == 1
        assert result[0].observation_count == 3
        assert result[0].cost_usd == Decimal("2")

    def test_window_start_end_from_observed_at(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(observed_at=_NOW - dt.timedelta(hours=2)),
            _obs(observed_at=_NOW - dt.timedelta(hours=1)),
            _obs(observed_at=_NOW),
        ]
        result = builder.build(obs)
        assert result[0].window_start == _NOW - dt.timedelta(hours=2)
        assert result[0].window_end == _NOW

    def test_weighted_composite_prefers_heavier_dimension(self) -> None:
        cheap_slow = _obs(provider_id="a", cost_usd="1", latency_ms=200, quality=0.5)
        expensive_fast = _obs(provider_id="b", cost_usd="10", latency_ms=50, quality=0.5)

        cost_heavy = ScorecardBuilder(cost_weight=10, latency_weight=1, quality_weight=1)
        cost_result = cost_heavy.build([cheap_slow, expensive_fast])
        assert cost_result[0].provider_id == "a"  # cheap wins

        latency_heavy = ScorecardBuilder(cost_weight=1, latency_weight=10, quality_weight=1)
        latency_result = latency_heavy.build([cheap_slow, expensive_fast])
        assert latency_result[0].provider_id == "b"  # fast wins

    def test_geometric_composite_penalises_any_zero(self) -> None:
        builder = ScorecardBuilder(composite_method="geometric")
        obs = [
            _obs(provider_id="a", cost_usd="1", latency_ms=100, quality=1.0),
            _obs(provider_id="b", cost_usd="10", latency_ms=200, quality=0.0),
        ]
        result = builder.build(obs)
        a = next(r for r in result if r.provider_id == "a")
        b = next(r for r in result if r.provider_id == "b")
        assert b.composite_score == 0.0
        assert a.composite_score > 0.0

    def test_median_aggregator(self) -> None:
        builder = ScorecardBuilder(
            cost_aggregator="median",
            latency_aggregator="median",
            quality_aggregator="median",
        )
        obs = [
            _obs(cost_usd="1", latency_ms=10, quality=0.1),
            _obs(cost_usd="2", latency_ms=20, quality=0.2),
            _obs(cost_usd="100", latency_ms=1000, quality=1.0),
        ]
        result = builder.build(obs)
        assert result[0].cost_usd == Decimal("2")
        assert result[0].latency_ms == 20.0
        assert result[0].quality == 0.2

    def test_p95_aggregator(self) -> None:
        builder = ScorecardBuilder(latency_aggregator="p95")
        obs = [
            _obs(latency_ms=10),
            _obs(latency_ms=20),
            _obs(latency_ms=30),
            _obs(latency_ms=40),
            _obs(latency_ms=50),
        ]
        result = builder.build(obs)
        # p95 of 5 items = ceil(0.95*5)-1 = 4 -> 50
        assert result[0].latency_ms == 50.0

    def test_sum_aggregator(self) -> None:
        builder = ScorecardBuilder(cost_aggregator="sum")
        obs = [
            _obs(cost_usd="1.5"),
            _obs(cost_usd="2.5"),
        ]
        result = builder.build(obs)
        assert result[0].cost_usd == Decimal("4")

    def test_same_values_get_full_normalised_score(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(provider_id="a", cost_usd="5", latency_ms=100, quality=0.5),
            _obs(provider_id="b", cost_usd="5", latency_ms=100, quality=0.5),
        ]
        result = builder.build(obs)
        for r in result:
            assert r.cost_normalized == 1.0
            assert r.latency_normalized == 1.0
            assert r.quality_normalized == 1.0
            assert r.composite_score == 1.0

    def test_rank_breaks_ties_by_provider_id(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(provider_id="b", cost_usd="5"),
            _obs(provider_id="a", cost_usd="5"),
        ]
        result = builder.build(obs)
        assert result[0].provider_id == "a"
        assert result[0].rank == 1
        assert result[1].provider_id == "b"
        assert result[1].rank == 2

    def test_card_to_dict_roundtrips(self) -> None:
        builder = ScorecardBuilder()
        obs = [_obs()]
        result = builder.build(obs)
        d = result[0].to_dict()
        assert d["provider_id"] == "p1"
        assert d["capability_id"] == "c1"
        assert d["cost_usd"] == 1.0
        assert d["latency_ms"] == 100.0
        assert d["quality"] == 1.0
        assert d["composite_score"] == 1.0
        assert d["rank"] == 1
        assert d["observation_count"] == 1


class TestScorecardBuilderValidation:
    def test_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError, match="weights must be non-negative"):
            ScorecardBuilder(cost_weight=-1)

    def test_invalid_composite_method_raises(self) -> None:
        with pytest.raises(ValueError, match="composite_method must be"):
            ScorecardBuilder(composite_method="harmonic")  # type: ignore[arg-type]  # reason: intentionally invalid literal to exercise validation

    def test_invalid_aggregator_raises(self) -> None:
        with pytest.raises(ValueError, match="cost_aggregator must be one of"):
            ScorecardBuilder(cost_aggregator="mode")  # type: ignore[arg-type]  # reason: intentionally invalid literal to exercise validation


class TestObservationsFromWorkloads:
    def test_basic_workload_conversion(self) -> None:
        w = SimpleNamespace(
            provider_id="p1",
            capability_id="c1",
            cost_actual_usd=Decimal("2.50"),
            execution_ms=150,
            error=None,
            completed_at=_NOW,
        )
        result = observations_from_workloads([w])
        assert len(result) == 1
        assert result[0].provider_id == "p1"
        assert result[0].capability_id == "c1"
        assert result[0].cost_usd == Decimal("2.50")
        assert result[0].latency_ms == 150.0
        assert result[0].quality == 1.0
        assert result[0].observed_at == _NOW

    def test_error_workload_gets_zero_quality(self) -> None:
        w = SimpleNamespace(
            provider_id="p1",
            capability_id="c1",
            cost_actual_usd=Decimal("1"),
            execution_ms=100,
            error={"message": "boom"},
            completed_at=None,
        )
        result = observations_from_workloads([w])
        assert result[0].quality == 0.0

    def test_custom_quality_extractor(self) -> None:
        w = SimpleNamespace(
            provider_id="p1",
            capability_id="c1",
            cost_actual_usd=Decimal("1"),
            execution_ms=100,
            error=None,
            score=0.75,
        )
        result = observations_from_workloads([w], quality_extractor=lambda x: x.score)
        assert result[0].quality == 0.75

    def test_missing_provider_or_capability_skipped(self) -> None:
        w1 = SimpleNamespace(provider_id="p1", capability_id=None)
        w2 = SimpleNamespace(provider_id=None, capability_id="c1")
        result = observations_from_workloads([w1, w2])
        assert len(result) == 0

    def test_non_finite_latency_clamped_to_zero(self) -> None:
        w = SimpleNamespace(
            provider_id="p1",
            capability_id="c1",
            cost_actual_usd=Decimal("1"),
            execution_ms=float("inf"),
            error=None,
            completed_at=None,
        )
        result = observations_from_workloads([w])
        assert result[0].latency_ms == 0.0

    def test_quality_clamped_to_unit_interval(self) -> None:
        w = SimpleNamespace(
            provider_id="p1",
            capability_id="c1",
            cost_actual_usd=Decimal("1"),
            execution_ms=100,
            error=None,
            completed_at=None,
        )

        def high(_: Any) -> float:
            return 1.5

        def low(_: Any) -> float:
            return -0.5

        assert observations_from_workloads([w], quality_extractor=high)[0].quality == 1.0
        assert observations_from_workloads([w], quality_extractor=low)[0].quality == 0.0

    def test_string_cost_converted(self) -> None:
        w = SimpleNamespace(
            provider_id="p1",
            capability_id="c1",
            cost_actual_usd="3.33",
            execution_ms=100,
            error=None,
            completed_at=None,
        )
        result = observations_from_workloads([w])
        assert result[0].cost_usd == Decimal("3.33")

    def test_none_cost_defaults_to_zero(self) -> None:
        w = SimpleNamespace(
            provider_id="p1",
            capability_id="c1",
            cost_actual_usd=None,
            execution_ms=100,
            error=None,
            completed_at=None,
        )
        result = observations_from_workloads([w])
        assert result[0].cost_usd == Decimal("0")


class TestScorecardBuilderEdgeCases:
    def test_zero_weight_dimensions_ignored_in_composite(self) -> None:
        builder = ScorecardBuilder(cost_weight=0, latency_weight=0, quality_weight=1)
        obs = [
            _obs(provider_id="a", cost_usd="1", latency_ms=50, quality=0.2),
            _obs(provider_id="b", cost_usd="10", latency_ms=200, quality=0.8),
        ]
        result = builder.build(obs)
        # Only quality matters; b has higher quality
        assert result[0].provider_id == "b"
        assert result[0].composite_score == pytest.approx(1.0)
        assert result[1].composite_score == pytest.approx(0.0)

    def test_all_zero_weights_composite_is_zero(self) -> None:
        builder = ScorecardBuilder(cost_weight=0, latency_weight=0, quality_weight=0)
        obs = [_obs()]
        result = builder.build(obs)
        assert result[0].composite_score == 0.0

    def test_observations_without_observed_at(self) -> None:
        builder = ScorecardBuilder()
        obs = [
            _obs(observed_at=None),
            _obs(observed_at=None),
        ]
        result = builder.build(obs)
        assert result[0].window_start is None
        assert result[0].window_end is None

    def test_decimal_cost_preserves_precision(self) -> None:
        builder = ScorecardBuilder(cost_aggregator="sum")
        obs = [
            _obs(cost_usd=Decimal("0.000001")),
            _obs(cost_usd=Decimal("0.000002")),
        ]
        result = builder.build(obs)
        assert result[0].cost_usd == Decimal("0.000003")

    def test_negative_quality_extracted_is_clamped(self) -> None:
        w = SimpleNamespace(
            provider_id="p1",
            capability_id="c1",
            cost_actual_usd=Decimal("1"),
            execution_ms=100,
            error=None,
            completed_at=None,
        )
        result = observations_from_workloads([w], quality_extractor=lambda _: -5.0)
        assert result[0].quality == 0.0
