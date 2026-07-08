"""Property tests for price-latency arbitrage invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.routing.arbitrage import ArbitrageOption, select_arbitrage_option

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
lambda_weights = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("10"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


@st.composite
def arbitrage_options(draw: st.DrawFn) -> tuple[ArbitrageOption, ...]:
    count = draw(st.integers(min_value=1, max_value=8))
    options: list[ArbitrageOption] = []
    for index in range(count):
        options.append(
            ArbitrageOption(
                provider_id=f"provider-{index}",
                gpu=f"gpu-{index}",
                price=draw(decimal_amounts),
                latency_ms=draw(latencies),
            )
        )
    return tuple(options)


@settings(max_examples=50)
@given(options=arbitrage_options(), lambda_weight=lambda_weights)
def test_selected_objective_is_no_greater_than_every_candidate(
    options: tuple[ArbitrageOption, ...],
    lambda_weight: Decimal,
) -> None:
    selected = select_arbitrage_option(options, lambda_weight=lambda_weight)

    for option in options:
        assert selected.objective <= option.price + lambda_weight * option.latency_ms


@settings(max_examples=50)
@given(
    options=arbitrage_options(),
    low_lambda=lambda_weights,
    lambda_delta=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("10"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    ),
)
def test_selected_latency_never_increases_as_lambda_increases(
    options: tuple[ArbitrageOption, ...],
    low_lambda: Decimal,
    lambda_delta: Decimal,
) -> None:
    high_lambda = low_lambda + lambda_delta

    low_selection = select_arbitrage_option(options, lambda_weight=low_lambda)
    high_selection = select_arbitrage_option(options, lambda_weight=high_lambda)

    assert high_selection.option.latency_ms <= low_selection.option.latency_ms
