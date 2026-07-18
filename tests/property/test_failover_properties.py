"""Property tests for failover target selection invariants."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.core.enums import CapabilitySource, ProviderType
from pitwall.core.models import Provider as ProviderRecord
from pitwall.routing.failover import (
    FailoverCapacityMarket,
    FailoverTarget,
    select_on_demand_failover_target,
)

pytestmark = pytest.mark.property

_NOW = dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC)

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


def _provider_record(provider_id: str) -> ProviderRecord:
    return ProviderRecord(
        id=provider_id,
        capability_id="cap_gpu_lease",
        name=provider_id,
        provider_type=ProviderType.POD_LEASE,
        config={},
        priority=1,
        source=CapabilitySource.API,
        updated_at=_NOW,
    )


@st.composite
def failover_targets(draw: st.DrawFn) -> tuple[FailoverTarget, ...]:
    count = draw(st.integers(min_value=1, max_value=8))
    targets: list[FailoverTarget] = []
    on_demand_index = draw(st.integers(min_value=0, max_value=count - 1))
    for index in range(count):
        market = (
            FailoverCapacityMarket.ON_DEMAND
            if index == on_demand_index
            else draw(
                st.sampled_from(
                    [
                        FailoverCapacityMarket.ON_DEMAND,
                        FailoverCapacityMarket.SPOT,
                        FailoverCapacityMarket.PREEMPTIBLE,
                    ]
                )
            )
        )
        targets.append(
            FailoverTarget(
                provider_plugin_id="on-demand",
                provider_record=_provider_record(f"provider-{index}"),
                credentials={"token": "test-token"},
                market=market,
                gpu=f"gpu-{index}",
                price=draw(decimal_amounts),
                latency_ms=draw(latencies),
            )
        )
    return tuple(targets)


@settings(max_examples=50)
@given(targets=failover_targets(), lambda_weight=lambda_weights)
def test_selected_on_demand_target_has_minimal_arbitrage_objective(
    targets: tuple[FailoverTarget, ...],
    lambda_weight: Decimal,
) -> None:
    selection = select_on_demand_failover_target(targets, lambda_weight=lambda_weight)

    assert selection.target.market is FailoverCapacityMarket.ON_DEMAND
    for target in targets:
        if target.market is not FailoverCapacityMarket.ON_DEMAND:
            continue
        objective = target.price + lambda_weight * target.latency_ms
        assert selection.score.objective <= objective
