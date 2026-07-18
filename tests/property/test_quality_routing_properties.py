"""Property tests for quality-aware routing invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.routing.quality_routing import (
    QualityRoutingOption,
    QualityRoutingPolicy,
    select_quality_routing_option,
)

pytestmark = pytest.mark.property


quality_scores = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)
costs = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)
latencies = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=3,
)
weights = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("10"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


@st.composite
def quality_options(draw: st.DrawFn) -> tuple[QualityRoutingOption, ...]:
    count = draw(st.integers(min_value=1, max_value=8))
    options: list[QualityRoutingOption] = []
    for index in range(count):
        options.append(
            QualityRoutingOption(
                provider_id=f"provider-{index}",
                model_id=f"model-{index}",
                quality=draw(quality_scores),
                cost_usd=draw(costs),
                latency_ms=draw(latencies),
            )
        )
    return tuple(options)


@settings(max_examples=50)
@given(options=quality_options())
def test_default_selection_quality_is_no_lower_than_any_candidate(
    options: tuple[QualityRoutingOption, ...],
) -> None:
    selected = select_quality_routing_option(options)

    for option in options:
        assert selected.option.quality >= option.quality


@settings(max_examples=50)
@given(
    options=quality_options(),
    quality_weight=weights.filter(lambda value: value > 0),
    cost_weight=weights,
    latency_weight=weights,
)
def test_weighted_selection_objective_is_no_lower_than_any_candidate(
    options: tuple[QualityRoutingOption, ...],
    quality_weight: Decimal,
    cost_weight: Decimal,
    latency_weight: Decimal,
) -> None:
    policy = QualityRoutingPolicy(
        quality_weight=quality_weight,
        cost_weight=cost_weight,
        latency_weight=latency_weight,
    )

    selected = select_quality_routing_option(options, policy=policy)

    for option in options:
        objective = (
            quality_weight * option.quality
            - cost_weight * option.cost_usd
            - latency_weight * option.latency_ms
        )
        assert selected.objective >= objective
