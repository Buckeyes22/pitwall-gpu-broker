"""Property-based tests for the cost SLO governor."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.cost.circuit_breaker import (
    BudgetCircuitBreaker,
)
from pitwall.cost.slo_governor import CostGovernor, CostSLO

pytestmark = pytest.mark.property

_NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)

_MONEY = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1_000_000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)

_RATE = st.decimals(
    min_value=Decimal("0.000001"),
    max_value=Decimal("1_000_000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


def _slo() -> CostSLO:
    return CostSLO(
        per_day_target_usd=Decimal("100.00"),
        per_request_p95_usd=Decimal("10.00"),
        throttle_threshold=Decimal("0.80"),
        defer_threshold=Decimal("1.00"),
    )


@given(burn_rate=_RATE)
def test_action_is_always_valid(burn_rate: Decimal) -> None:
    governor = CostGovernor()
    decision = governor.evaluate(
        slo=_slo(),
        burn_rate_usd_per_day=burn_rate,
        now=_NOW,
    )
    assert decision.action in {"allow", "throttle", "defer"}
    assert decision.velocity_ratio >= 0


@given(burn_rate=_RATE)
def test_breaker_block_always_defers(burn_rate: Decimal) -> None:
    governor = CostGovernor()
    breaker = BudgetCircuitBreaker()
    breaker_decision = breaker.evaluate(
        budget_usd=Decimal("1000.00"),
        mtd_spend_usd=Decimal("1000.00"),
        now=_NOW,
    )
    assert breaker_decision.action == "block"

    decision = governor.evaluate(
        slo=_slo(),
        burn_rate_usd_per_day=burn_rate,
        now=_NOW,
        breaker_decision=breaker_decision,
    )
    assert decision.action == "defer"


@given(
    rate1=_RATE,
    rate2=_RATE,
)
def test_higher_velocity_is_not_weaker_action(
    rate1: Decimal,
    rate2: Decimal,
) -> None:
    governor = CostGovernor()
    slo = _slo()
    d1 = governor.evaluate(
        slo=slo,
        burn_rate_usd_per_day=rate1,
        now=_NOW,
    )
    d2 = governor.evaluate(
        slo=slo,
        burn_rate_usd_per_day=rate2,
        now=_NOW,
    )

    _SEVERITY = {"allow": 0, "throttle": 1, "defer": 2}
    if rate2 >= rate1:
        assert _SEVERITY[d2.action] >= _SEVERITY[d1.action]
