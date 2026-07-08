"""Property-based tests for cost estimators.

Invariants (all over Decimal, never float):
    1. estimate() is always >= 0 for valid inputs
    2. per-second cost is monotonic non-decreasing in the rate
    3. per-token cost is monotonic non-decreasing in input token count
    4. output is always quantized to 6 decimal places (NUMERIC(12,6))
    5. negative rates always raise ValueError (never a silent wrong cost)
    6. get_estimator round-trips over every registered cost mode

API grounding (verified against src/pitwall/cost/estimator.py 2026-05-30):
    estimate(capability, provider_cost, payload) -> Decimal   # POSITIONAL
    provider_cost is the FLAT cost mapping, e.g. {"per_second_active": "0.000123"}
    (matches tests/cost/test_estimator.py). _usd() quantizes to Decimal("0.000001")
    with ROUND_HALF_UP; _non_negative_decimal() raises ValueError on negatives/NaN.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import CapabilityClass, CapabilitySource
from pitwall.core.models import Capability
from pitwall.cost.estimator import (
    PerRequestEstimator,
    PerSecondEstimator,
    PerTokenEstimator,
    get_estimator,
)

pytestmark = pytest.mark.property

_QUANTUM = Decimal("0.000001")


def _capability(cost_mode: str, execution_timeout_ms: int = 60_000) -> Capability:
    return Capability(
        id="cap_prop",
        name="prop.test",
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode=cost_mode,
        source=CapabilitySource.API,
        enabled=True,
        created_at="2026-05-28T12:00:00+00:00",
        updated_at="2026-05-28T12:00:00+00:00",
        defaults={"execution_timeout_ms": execution_timeout_ms},
    )


# ---- strategies ---------------------------------------------------------
rate_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)
timeout_ms_strategy = st.integers(min_value=1, max_value=86_400_000)
token_strategy = st.integers(min_value=1, max_value=10_000_000)


@given(rate=rate_strategy, timeout_ms=timeout_ms_strategy)
def test_per_second_nonnegative_quantized_monotonic(rate: Decimal, timeout_ms: int) -> None:
    cap = _capability("per_second", execution_timeout_ms=timeout_ms)
    est = PerSecondEstimator()
    out = est.estimate(cap, {"per_second_active": str(rate)}, {})
    assert out >= 0
    assert out == out.quantize(_QUANTUM)
    # monotonic in rate: a strictly higher rate never costs less
    higher = est.estimate(cap, {"per_second_active": str(rate + 1)}, {})
    assert higher >= out


@given(in_tok=token_strategy, out_tok=token_strategy)
def test_per_token_nonnegative_quantized_monotonic(in_tok: int, out_tok: int) -> None:
    cap = _capability("per_token")
    provider_cost = {
        "per_million_input_tokens": "1.0",
        "per_million_output_tokens": "2.0",
    }
    est = PerTokenEstimator()
    base = est.estimate(cap, provider_cost, {"input_tokens": in_tok, "output_tokens": out_tok})
    more = est.estimate(
        cap, provider_cost, {"input_tokens": in_tok + 100, "output_tokens": out_tok}
    )
    assert base >= 0
    assert base == base.quantize(_QUANTUM)
    assert more >= base


@given(req_cost=rate_strategy)
def test_per_request_flat_nonnegative_quantized(req_cost: Decimal) -> None:
    cap = _capability("per_request")
    est = PerRequestEstimator()
    out = est.estimate(cap, {"per_request": str(req_cost)}, {})
    assert out >= 0
    assert out == out.quantize(_QUANTUM)


@given(
    bad=st.decimals(max_value=Decimal("-0.000001"), allow_nan=False, allow_infinity=False, places=6)
)
def test_negative_per_second_rate_raises(bad: Decimal) -> None:
    cap = _capability("per_second")
    est = PerSecondEstimator()
    with pytest.raises(ValueError):
        est.estimate(cap, {"per_second_active": str(bad)}, {})


@given(mode=st.sampled_from(["per_second", "per_request", "per_token"]))
def test_get_estimator_roundtrips_registry(mode: str) -> None:
    est = get_estimator(mode)
    assert est is not None
    # the returned estimator estimates without error for a valid flat cost
    cap = _capability(mode)
    cost = {
        "per_second": {"per_second_active": "0.001"},
        "per_request": {"per_request": "0.001"},
        "per_token": {
            "per_million_input_tokens": "1.0",
            "per_million_output_tokens": "2.0",
        },
    }[mode]
    payload = {"input_tokens": 10, "output_tokens": 10} if mode == "per_token" else {}
    out = est.estimate(cap, cost, payload)
    assert out >= 0


def test_get_estimator_unknown_mode_raises() -> None:
    with pytest.raises((ValueError, KeyError)):
        get_estimator("nonsense_mode")
