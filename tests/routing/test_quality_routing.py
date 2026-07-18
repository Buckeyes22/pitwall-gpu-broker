"""Hermetic tests for quality-aware routing selection."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from pitwall.observability.scorecards import EntityScorecard
from pitwall.routing.quality_routing import (
    QualityRoutingOption,
    QualityRoutingPolicy,
    quality_option_from_scorecard,
    score_quality_routing_option,
    select_quality_routing_option,
    sort_quality_routing_options,
)


def _option(
    provider_id: str,
    *,
    model_id: str = "model-a",
    quality: str,
    cost_usd: str,
    latency_ms: str,
) -> QualityRoutingOption:
    return QualityRoutingOption(
        provider_id=provider_id,
        model_id=model_id,
        quality=Decimal(quality),
        cost_usd=Decimal(cost_usd),
        latency_ms=Decimal(latency_ms),
    )


def test_selects_highest_quality_within_cost_and_latency_constraints() -> None:
    options = (
        _option("excellent-too-expensive", quality="0.99", cost_usd="0.900", latency_ms="100"),
        _option("best-eligible", quality="0.91", cost_usd="0.200", latency_ms="250"),
        _option("lower-quality", quality="0.75", cost_usd="0.050", latency_ms="120"),
    )
    policy = QualityRoutingPolicy(
        max_cost_usd=Decimal("0.250"),
        max_latency_ms=Decimal("300"),
    )

    selected = select_quality_routing_option(options, policy=policy)

    assert selected.option.provider_id == "best-eligible"
    assert selected.objective == Decimal("0.91")


def test_without_constraints_selects_highest_quality_even_when_costlier() -> None:
    options = (
        _option("cheap-fast", quality="0.60", cost_usd="0.010", latency_ms="50"),
        _option("expensive-best", quality="0.95", cost_usd="0.400", latency_ms="500"),
    )

    selected = select_quality_routing_option(options)

    assert selected.option.provider_id == "expensive-best"


def test_weighted_policy_can_trade_quality_for_lower_cost_and_latency() -> None:
    options = (
        _option("high-quality-slow", quality="0.95", cost_usd="0.400", latency_ms="500"),
        _option("balanced", quality="0.90", cost_usd="0.050", latency_ms="100"),
    )
    policy = QualityRoutingPolicy(
        quality_weight=Decimal("1"),
        cost_weight=Decimal("1"),
        latency_weight=Decimal("0.001"),
    )

    selected = select_quality_routing_option(options, policy=policy)

    assert selected.option.provider_id == "balanced"
    assert selected.objective == Decimal("0.750")


def test_sorting_tie_breaks_by_quality_cost_latency_provider_and_model() -> None:
    options = (
        _option("highest-quality", quality="0.81", cost_usd="9.00", latency_ms="900"),
        _option("lower-cost", quality="0.80", cost_usd="0.09", latency_ms="200"),
        _option("lower-latency", quality="0.80", cost_usd="0.10", latency_ms="50"),
        _option(
            "provider-b", model_id="model-a", quality="0.80", cost_usd="0.10", latency_ms="100"
        ),
        _option(
            "provider-a", model_id="model-z", quality="0.80", cost_usd="0.10", latency_ms="100"
        ),
        _option(
            "provider-a", model_id="model-a", quality="0.80", cost_usd="0.10", latency_ms="100"
        ),
    )

    ranked = sort_quality_routing_options(options)

    assert [(score.provider_id, score.model_id) for score in ranked] == [
        ("highest-quality", "model-a"),
        ("lower-cost", "model-a"),
        ("lower-latency", "model-a"),
        ("provider-a", "model-a"),
        ("provider-a", "model-z"),
        ("provider-b", "model-a"),
    ]


def test_select_raises_when_no_candidate_meets_constraints() -> None:
    options = (
        _option("too-expensive", quality="0.90", cost_usd="0.30", latency_ms="100"),
        _option("too-slow", quality="0.90", cost_usd="0.10", latency_ms="400"),
    )
    policy = QualityRoutingPolicy(
        max_cost_usd=Decimal("0.20"),
        max_latency_ms=Decimal("300"),
    )

    with pytest.raises(ValueError, match="no options satisfy quality routing policy"):
        select_quality_routing_option(options, policy=policy)


def test_empty_options_raise_value_error() -> None:
    with pytest.raises(ValueError, match="options must contain at least one option"):
        select_quality_routing_option(())


def test_scorecard_candidate_uses_normalized_quality_cost_and_latency() -> None:
    card = EntityScorecard(
        provider_id="runpod-l4",
        capability_id="embedding",
        cost_usd=Decimal("0.123"),
        latency_ms=250.5,
        quality=0.72,
        cost_normalized=0.5,
        latency_normalized=0.6,
        quality_normalized=0.84,
        composite_score=0.66,
        rank=2,
        observation_count=10,
        window_start=dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 6, 2, 13, 0, tzinfo=dt.UTC),
    )

    option = quality_option_from_scorecard(card, model_id="bge-m3")

    assert option.provider_id == "runpod-l4"
    assert option.model_id == "bge-m3"
    assert option.quality == Decimal("0.84")
    assert option.cost_usd == Decimal("0.123")
    assert option.latency_ms == Decimal("250.5")


@pytest.mark.parametrize(
    ("option", "policy", "match"),
    [
        (
            _option("bad-quality", quality="1.01", cost_usd="0.10", latency_ms="100"),
            QualityRoutingPolicy(),
            "quality must be between 0 and 1",
        ),
        (
            _option("bad-cost", quality="0.90", cost_usd="-0.01", latency_ms="100"),
            QualityRoutingPolicy(),
            "cost_usd must be non-negative",
        ),
        (
            _option("", quality="0.90", cost_usd="0.10", latency_ms="100"),
            QualityRoutingPolicy(),
            "provider_id must be a non-empty string",
        ),
        (
            _option("bad-weight", quality="0.90", cost_usd="0.10", latency_ms="100"),
            QualityRoutingPolicy(quality_weight=Decimal("-0.1")),
            "quality_weight must be non-negative",
        ),
    ],
)
def test_invalid_quality_routing_inputs_are_rejected(
    option: QualityRoutingOption,
    policy: QualityRoutingPolicy,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        score_quality_routing_option(option, policy=policy)
