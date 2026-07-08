"""Property tests for cascade routing cost and stop-point invariants."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.routing.cascade import (
    CascadeGateDecision,
    CascadeProviderRequest,
    CascadeRoutingError,
    CascadeTier,
    route_with_cascade,
)

pytestmark = pytest.mark.property


@dataclass(frozen=True, slots=True)
class FakeProvider:
    id: str


@settings(max_examples=30)
@given(
    gate_passes=st.lists(st.booleans(), min_size=1, max_size=8),
    max_attempts=st.integers(min_value=1, max_value=8),
)
@pytest.mark.anyio
async def test_cascade_attempts_stop_at_first_gate_pass_and_sum_attempted_costs(
    gate_passes: list[bool],
    max_attempts: int,
) -> None:
    tiers = [
        CascadeTier(
            provider=FakeProvider(id=f"provider_{index}"),
            estimated_cost_usd=Decimal(index + 1),
        )
        for index in range(len(gate_passes))
    ]

    async def call(provider: FakeProvider) -> str:
        return provider.id

    def gate(provider: FakeProvider, value: str) -> CascadeGateDecision:
        index = int(value.removeprefix("provider_"))
        return CascadeGateDecision(passed=gate_passes[index])

    expected_count = _expected_attempt_count(gate_passes, max_attempts)
    expected_cost = sum(
        (tiers[index].estimated_cost_usd for index in range(expected_count)),
        start=Decimal("0"),
    )

    request = CascadeProviderRequest(
        tiers=tiers,
        call_provider=call,
        quality_gate=gate,
        max_attempts=max_attempts,
    )
    if any(gate_passes[: min(len(gate_passes), max_attempts)]):
        result = await route_with_cascade(request)

        assert len(result.attempts) == expected_count
        assert result.total_cost_usd == expected_cost
        assert result.attempts[-1].gate.passed is True
        assert result.attempted_provider_ids == tuple(
            f"provider_{index}" for index in range(expected_count)
        )
    else:
        with pytest.raises(CascadeRoutingError) as exc_info:
            await route_with_cascade(request)

        assert len(exc_info.value.attempts) == expected_count
        assert exc_info.value.total_cost_usd == expected_cost
        assert all(not attempt.gate.passed for attempt in exc_info.value.attempts)


def _expected_attempt_count(gate_passes: list[bool], max_attempts: int) -> int:
    bound = min(len(gate_passes), max_attempts)
    for index, passed in enumerate(gate_passes[:bound], start=1):
        if passed:
            return index
    return bound
