"""Property tests for carbon-aware arbitrage invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.routing.arbitrage import ArbitrageOption, select_arbitrage_option
from pitwall.routing.carbon import StaticCarbonIntensitySource

pytestmark = pytest.mark.property


decimal_amounts = st.decimals(
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
carbon_intensities = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000"),
    allow_nan=False,
    allow_infinity=False,
    places=3,
)
carbon_weights = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


@settings(max_examples=50)
@given(
    price=decimal_amounts,
    latency_ms=latencies,
    low_carbon=carbon_intensities,
    carbon_gap=carbon_intensities,
    low_weight=carbon_weights,
    weight_delta=carbon_weights,
)
def test_selected_carbon_never_increases_as_carbon_weight_increases(
    price: Decimal,
    latency_ms: Decimal,
    low_carbon: Decimal,
    carbon_gap: Decimal,
    low_weight: Decimal,
    weight_delta: Decimal,
) -> None:
    high_carbon = low_carbon + carbon_gap
    source = StaticCarbonIntensitySource(
        provider_region_intensities={
            ("a-high-carbon", "DIRTY"): high_carbon,
            ("z-low-carbon", "CLEAN"): low_carbon,
        },
    )
    options = (
        ArbitrageOption(
            provider_id="a-high-carbon",
            gpu="NVIDIA L4",
            price=price,
            latency_ms=latency_ms,
            region="DIRTY",
        ),
        ArbitrageOption(
            provider_id="z-low-carbon",
            gpu="NVIDIA L4",
            price=price,
            latency_ms=latency_ms,
            region="CLEAN",
        ),
    )

    low_selection = select_arbitrage_option(
        options,
        lambda_weight=Decimal("0"),
        carbon_weight=low_weight,
        carbon_source=source,
    )
    high_selection = select_arbitrage_option(
        options,
        lambda_weight=Decimal("0"),
        carbon_weight=low_weight + weight_delta,
        carbon_source=source,
    )

    assert (
        high_selection.carbon_intensity_gco2_per_kwh <= low_selection.carbon_intensity_gco2_per_kwh
    )
