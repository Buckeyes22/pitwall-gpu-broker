"""Hermetic tests for price-latency arbitrage selection."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pitwall.routing.arbitrage import (
    ArbitrageOption,
    score_arbitrage_option,
    select_arbitrage_option,
    sort_arbitrage_options,
)
from pitwall.routing.carbon import StaticCarbonIntensitySource


def _option(
    provider_id: str,
    *,
    gpu: str = "NVIDIA L4",
    price: str,
    latency_ms: str,
    region: str | None = None,
) -> ArbitrageOption:
    return ArbitrageOption(
        provider_id=provider_id,
        gpu=gpu,
        price=Decimal(price),
        latency_ms=Decimal(latency_ms),
        region=region,
    )


def test_score_uses_decimal_cost_plus_lambda_times_latency() -> None:
    option = _option("runpod-l4", price="0.120000", latency_ms="250")

    score = score_arbitrage_option(option, lambda_weight=Decimal("0.0002"))

    assert score.option == option
    assert score.lambda_weight == Decimal("0.0002")
    assert score.cost_component == Decimal("0.120000")
    assert score.latency_component == Decimal("0.0500")
    assert score.objective == Decimal("0.170000")


def test_zero_lambda_selects_cheapest_option() -> None:
    options = (
        _option("fast-expensive", gpu="NVIDIA H100", price="0.500000", latency_ms="80"),
        _option("cheap-slow", gpu="NVIDIA L4", price="0.090000", latency_ms="900"),
        _option("middle", gpu="NVIDIA A10", price="0.200000", latency_ms="200"),
    )

    selected = select_arbitrage_option(options, lambda_weight=Decimal("0"))

    assert selected.option.provider_id == "cheap-slow"
    assert selected.objective == Decimal("0.090000")


def test_large_lambda_selects_fastest_option() -> None:
    options = (
        _option("cheap-slow", gpu="NVIDIA L4", price="0.010000", latency_ms="900"),
        _option("fast-expensive", gpu="NVIDIA H100", price="9.000000", latency_ms="10"),
        _option("middle", gpu="NVIDIA A10", price="0.200000", latency_ms="200"),
    )

    selected = select_arbitrage_option(options, lambda_weight=Decimal("1000000"))

    assert selected.option.provider_id == "fast-expensive"


def test_lambda_sweep_moves_from_cheapest_to_fastest() -> None:
    options = (
        _option("cheap-slow", gpu="NVIDIA L4", price="0.100000", latency_ms="500"),
        _option("fast-expensive", gpu="NVIDIA H100", price="0.500000", latency_ms="100"),
    )

    low_lambda = select_arbitrage_option(options, lambda_weight=Decimal("0"))
    high_lambda = select_arbitrage_option(options, lambda_weight=Decimal("0.002"))

    assert low_lambda.option.provider_id == "cheap-slow"
    assert high_lambda.option.provider_id == "fast-expensive"


def test_carbon_weight_prefers_lower_carbon_when_cost_and_latency_match() -> None:
    source = StaticCarbonIntensitySource(
        region_intensities={
            "US-KS-2": Decimal("455"),
            "EU-SE-1": Decimal("45"),
        },
    )
    options = (
        _option("a-high-carbon", price="0.200000", latency_ms="100", region="US-KS-2"),
        _option("z-low-carbon", price="0.200000", latency_ms="100", region="EU-SE-1"),
    )

    selected = select_arbitrage_option(
        options,
        lambda_weight=Decimal("0"),
        carbon_weight=Decimal("0.0001"),
        carbon_source=source,
    )

    assert selected.option.provider_id == "z-low-carbon"
    assert selected.carbon_intensity_gco2_per_kwh == Decimal("45")
    assert selected.carbon_component == Decimal("0.0045")


def test_carbon_weight_sweep_moves_to_lower_carbon_provider() -> None:
    source = StaticCarbonIntensitySource(
        region_intensities={
            "US-KS-2": Decimal("455"),
            "EU-SE-1": Decimal("45"),
        },
    )
    options = (
        _option("a-high-carbon", price="0.100000", latency_ms="90", region="US-KS-2"),
        _option("z-low-carbon", price="0.130000", latency_ms="100", region="EU-SE-1"),
    )

    zero_carbon = select_arbitrage_option(
        options,
        lambda_weight=Decimal("0"),
        carbon_weight=Decimal("0"),
        carbon_source=source,
    )
    high_carbon = select_arbitrage_option(
        options,
        lambda_weight=Decimal("0"),
        carbon_weight=Decimal("0.0001"),
        carbon_source=source,
    )

    assert zero_carbon.option.provider_id == "a-high-carbon"
    assert high_carbon.option.provider_id == "z-low-carbon"


def test_sorting_is_deterministic_for_tied_objectives() -> None:
    options = (
        _option("provider-b", gpu="NVIDIA L4", price="0.200000", latency_ms="100"),
        _option("provider-a", gpu="NVIDIA L4", price="0.200000", latency_ms="100"),
    )

    sorted_scores = sort_arbitrage_options(options, lambda_weight=Decimal("0.001"))

    assert [score.option.provider_id for score in sorted_scores] == ["provider-a", "provider-b"]


def test_tie_breaks_prefer_lower_price_then_lower_latency() -> None:
    options = (
        _option("lower-latency", price="0.200000", latency_ms="100"),
        _option("lower-price", price="0.100000", latency_ms="200"),
    )

    selected = select_arbitrage_option(options, lambda_weight=Decimal("0.001"))

    assert selected.objective == Decimal("0.300000")
    assert selected.option.provider_id == "lower-price"


def test_selecting_empty_options_raises_value_error() -> None:
    with pytest.raises(ValueError, match="options must contain at least one option"):
        select_arbitrage_option((), lambda_weight=Decimal("0"))


@pytest.mark.parametrize(
    ("option", "lambda_weight", "match"),
    [
        (
            ArbitrageOption(
                provider_id="bad-price",
                gpu="NVIDIA L4",
                price=Decimal("-0.000001"),
                latency_ms=Decimal("1"),
            ),
            Decimal("0"),
            "price must be non-negative",
        ),
        (
            ArbitrageOption(
                provider_id="bad-latency",
                gpu="NVIDIA L4",
                price=Decimal("0.000001"),
                latency_ms=Decimal("-1"),
            ),
            Decimal("0"),
            "latency_ms must be non-negative",
        ),
        (
            _option("bad-lambda", price="0.000001", latency_ms="1"),
            Decimal("-0.000001"),
            "lambda_weight must be non-negative",
        ),
    ],
)
def test_negative_numeric_inputs_are_rejected(
    option: ArbitrageOption,
    lambda_weight: Decimal,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        score_arbitrage_option(option, lambda_weight=lambda_weight)


def test_negative_carbon_weight_is_rejected() -> None:
    option = _option("bad-carbon", price="0.000001", latency_ms="1", region="US-KS-2")

    with pytest.raises(ValueError, match="carbon_weight must be non-negative"):
        score_arbitrage_option(
            option,
            lambda_weight=Decimal("0"),
            carbon_weight=Decimal("-0.000001"),
        )


@pytest.mark.parametrize(
    ("provider_id", "gpu", "match"),
    [
        ("", "NVIDIA L4", "provider_id must be a non-empty string"),
        ("runpod-l4", "", "gpu must be a non-empty string"),
    ],
)
def test_option_identity_fields_are_validated(provider_id: str, gpu: str, match: str) -> None:
    option = ArbitrageOption(
        provider_id=provider_id,
        gpu=gpu,
        price=Decimal("0.000001"),
        latency_ms=Decimal("1"),
    )

    with pytest.raises(ValueError, match=match):
        score_arbitrage_option(option, lambda_weight=Decimal("0"))
