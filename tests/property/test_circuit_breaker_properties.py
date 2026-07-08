"""Property-based tests for the budget circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.cost.circuit_breaker import (
    BudgetCircuitBreaker,
)

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
    min_value=Decimal("0"),
    max_value=Decimal("1_000_000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


def _breaker() -> BudgetCircuitBreaker:
    return BudgetCircuitBreaker(
        headroom_trip_pct=Decimal("10.0"),
        runway_trip_hours=Decimal("24.0"),
        recovery_headroom_pct=Decimal("20.0"),
        recovery_runway_hours=Decimal("72.0"),
        downgrade_headroom_pct=Decimal("5.0"),
        cooldown_seconds=300.0,
    )


@given(
    budget=_MONEY,
    mtd_spend=_MONEY,
    burn_rate=_RATE,
)
def test_decision_action_is_always_valid(
    budget: Decimal,
    mtd_spend: Decimal,
    burn_rate: Decimal,
) -> None:
    breaker = _breaker()
    decision = breaker.evaluate(
        budget_usd=budget,
        mtd_spend_usd=mtd_spend,
        now=_NOW,
        burn_rate_usd_per_hour=burn_rate,
    )

    assert decision.action in {"allow", "downgrade", "block"}
    assert decision.state in {"closed", "open", "half-open"}
    assert decision.headroom_usd >= 0
    assert decision.headroom_pct >= 0
    if budget > 0:
        assert decision.headroom_pct <= Decimal("100")


@given(
    budget=_MONEY,
    mtd_spend=_MONEY,
)
def test_block_only_when_headroom_critically_low(
    budget: Decimal,
    mtd_spend: Decimal,
) -> None:
    breaker = _breaker()
    decision = breaker.evaluate(
        budget_usd=budget,
        mtd_spend_usd=mtd_spend,
        now=_NOW,
    )

    if decision.action == "block":
        # Block means headroom is at or below downgrade threshold (5 %)
        assert decision.headroom_pct <= Decimal("5.0")
    elif decision.action == "downgrade":
        # Downgrade means headroom is positive but stressed
        assert decision.headroom_pct >= 0


@given(
    budget=_MONEY,
    mtd_spend=_MONEY,
    burn_rate=_RATE,
)
def test_runway_monotonic_with_burn_rate(
    budget: Decimal,
    mtd_spend: Decimal,
    burn_rate: Decimal,
) -> None:
    breaker = _breaker()
    decision = breaker.evaluate(
        budget_usd=budget,
        mtd_spend_usd=mtd_spend,
        now=_NOW,
        burn_rate_usd_per_hour=burn_rate,
    )

    if burn_rate > 0 and budget > mtd_spend:
        assert decision.runway_hours is not None
        expected = (budget - mtd_spend) / burn_rate
        assert decision.runway_hours == expected
    elif burn_rate == 0:
        assert decision.runway_hours is None
