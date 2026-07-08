from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import pytest

from pitwall.cost.estimator import get_estimator
from pitwall.routing.planner import plan_route
from pitwall.routing.types import RoutingRequest
from tests.conftest import TEST_NOW, make_llm_capability, make_provider


def _assert_positive_finite_median(benchmark: Any) -> None:
    median_s = benchmark.stats["median"]
    assert median_s > 0
    assert math.isfinite(median_s)


@pytest.mark.benchmark
def test_per_second_estimator_micro_benchmark(benchmark: Any) -> None:
    estimator = get_estimator("per_second")
    capability = make_llm_capability(cost_mode="per_second")
    provider_cost = {"per_second_active": Decimal("0.000123")}

    def estimate_once() -> Decimal:
        return estimator.estimate(capability, provider_cost, {})

    result = benchmark.pedantic(estimate_once, rounds=500)

    assert result == Decimal("0.007380")
    _assert_positive_finite_median(benchmark)


@pytest.mark.benchmark
def test_plan_route_micro_benchmark(benchmark: Any) -> None:
    capability = make_llm_capability()
    provider = make_provider(endpoint_id="endpoint-route")
    request = RoutingRequest(
        capability_name=capability.name,
        capability_id=capability.id,
        payload_bytes=1024,
    )

    def plan_once() -> object:
        return plan_route(
            request,
            [provider],
            capability=capability,
            now=TEST_NOW,
        )

    result = benchmark.pedantic(plan_once, rounds=500)

    assert result.attempts[0].provider_id == provider.id
    assert result.eliminated == ()
    _assert_positive_finite_median(benchmark)
