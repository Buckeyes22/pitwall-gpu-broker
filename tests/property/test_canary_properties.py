"""Property tests for deterministic shadow/canary traffic selection."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.routing.canary import (
    CanaryMode,
    CanaryRoutingPolicy,
    select_canary_traffic,
    stable_traffic_bucket,
)

pytestmark = pytest.mark.property


_BUCKETS = st.floats(
    min_value=0.0,
    max_value=0.999999,
    allow_nan=False,
    allow_infinity=False,
)
_FRACTIONS = st.floats(
    min_value=0.0,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
)


@given(bucket=_BUCKETS, fraction=_FRACTIONS)
def test_candidate_selection_is_bucket_threshold(bucket: float, fraction: float) -> None:
    decision = select_canary_traffic(
        CanaryRoutingPolicy(
            mode=CanaryMode.CANARY,
            candidate_fraction=fraction,
            experiment_id="property",
        ),
        traffic_key="request",
        traffic_bucket=bucket,
    )

    assert decision.candidate_selected is (bucket < fraction)
    assert decision.bucket == bucket


@given(bucket=_BUCKETS, lower=_FRACTIONS, upper=_FRACTIONS)
def test_candidate_selection_is_monotonic_by_fraction(
    bucket: float,
    lower: float,
    upper: float,
) -> None:
    low_fraction = min(lower, upper)
    high_fraction = max(lower, upper)

    low = select_canary_traffic(
        CanaryRoutingPolicy(
            mode=CanaryMode.CANARY,
            candidate_fraction=low_fraction,
            experiment_id="property",
        ),
        traffic_key="request",
        traffic_bucket=bucket,
    )
    high = select_canary_traffic(
        CanaryRoutingPolicy(
            mode=CanaryMode.CANARY,
            candidate_fraction=high_fraction,
            experiment_id="property",
        ),
        traffic_key="request",
        traffic_bucket=bucket,
    )

    if low.candidate_selected:
        assert high.candidate_selected is True


@given(
    key=st.text(max_size=80),
    experiment_id=st.text(max_size=40),
)
def test_stable_bucket_is_deterministic_and_in_unit_interval(
    key: str,
    experiment_id: str,
) -> None:
    first = stable_traffic_bucket(key, experiment_id=experiment_id)
    second = stable_traffic_bucket(key, experiment_id=experiment_id)

    assert first == second
    assert math.isfinite(first)
    assert 0.0 <= first < 1.0
