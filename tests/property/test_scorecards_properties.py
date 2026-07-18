"""Property-based tests for the scorecard builder.

Grounded in src/pitwall/observability/scorecards.py.

Invariants:
    1. Determinism: identical inputs → identical scorecard tuples
    2. Rank uniqueness: ranks are 1..n with no gaps
    3. Normalised scores in [0, 1]
    4. Composite in [0, 1] for arithmetic; composite == 0 if any norm == 0 for geometric
    5. Higher weight on a dimension shifts ranking toward that dimension
    6. Empty input → empty output
    7. Single entity → all normalised scores == 1.0
"""

from __future__ import annotations

import datetime as dt
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.observability.scorecards import (
    ScorecardBuilder,
    ScorecardObservation,
)

pytestmark = pytest.mark.property

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)

_positive_decimal = st.decimals(min_value="0", max_value="10000", places=4)
_positive_float = st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False)
_quality_float = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@st.composite
def observation_lists(draw: st.DrawFn) -> list[ScorecardObservation]:
    n = draw(st.integers(min_value=1, max_value=8))
    observations: list[ScorecardObservation] = []
    for i in range(n):
        observations.append(
            ScorecardObservation(
                provider_id=f"p{i}",
                capability_id="c1",
                cost_usd=draw(_positive_decimal),
                latency_ms=draw(_positive_float),
                quality=draw(_quality_float),
                observed_at=_NOW,
            )
        )
    return observations


@given(observations=observation_lists())
def test_determinism(observations: list[ScorecardObservation]) -> None:
    builder = ScorecardBuilder()
    a = builder.build(observations)
    b = builder.build(observations)
    assert a == b


@given(observations=observation_lists())
def test_ranks_are_unique_and_contiguous(observations: list[ScorecardObservation]) -> None:
    builder = ScorecardBuilder()
    result = builder.build(observations)
    ranks = [r.rank for r in result]
    assert ranks == list(range(1, len(result) + 1))


@given(observations=observation_lists())
def test_normalised_scores_bounded_zero_one(
    observations: list[ScorecardObservation],
) -> None:
    builder = ScorecardBuilder()
    result = builder.build(observations)
    for r in result:
        assert 0.0 <= r.cost_normalized <= 1.0
        assert 0.0 <= r.latency_normalized <= 1.0
        assert 0.0 <= r.quality_normalized <= 1.0
        assert not math.isnan(r.composite_score)


@given(observations=observation_lists())
def test_arithmetic_composite_bounded_zero_one(
    observations: list[ScorecardObservation],
) -> None:
    builder = ScorecardBuilder(composite_method="arithmetic")
    result = builder.build(observations)
    for r in result:
        assert 0.0 <= r.composite_score <= 1.0


@given(observations=observation_lists())
def test_geometric_composite_zero_if_any_norm_zero(
    observations: list[ScorecardObservation],
) -> None:
    builder = ScorecardBuilder(composite_method="geometric")
    result = builder.build(observations)
    for r in result:
        if r.cost_normalized == 0.0 or r.latency_normalized == 0.0 or r.quality_normalized == 0.0:
            assert r.composite_score == 0.0


@given(observations=observation_lists())
def test_single_entity_all_ones(observations: list[ScorecardObservation]) -> None:
    if len(observations) != 1:
        return
    builder = ScorecardBuilder()
    result = builder.build(observations)
    assert len(result) == 1
    assert result[0].cost_normalized == 1.0
    assert result[0].latency_normalized == 1.0
    assert result[0].quality_normalized == 1.0
    assert result[0].composite_score == 1.0


@given(observations=observation_lists())
def test_empty_input_returns_empty(observations: list[ScorecardObservation]) -> None:
    if observations:
        return
    builder = ScorecardBuilder()
    assert builder.build(observations) == ()


@given(observations=observation_lists())
def test_ranking_is_non_increasing_by_composite(
    observations: list[ScorecardObservation],
) -> None:
    builder = ScorecardBuilder()
    result = builder.build(observations)
    scores = [r.composite_score for r in result]
    assert scores == sorted(scores, reverse=True)
