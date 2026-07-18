"""Property-based tests for BurnRateForecaster.

Invariants:
    1. burn_rate is always non-negative for non-negative inputs
    2. remaining_budget == budget - mtd_spend (clamped at 0)
    3. If burn_rate > 0 and remaining > 0, exhaustion > now
    4. Confidence is always in [0, 1]
    5. All-zero spend yields zero burn_rate and no exhaustion
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.finops.burn_rate import BurnRateForecaster, SpendPoint

pytestmark = pytest.mark.property

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)


# Strategies -----------------------------------------------------------------

decimal_usd = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("10000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
).map(lambda d: Decimal(str(d)))


spend_point_strategy = st.builds(
    SpendPoint,
    day=st.dates(min_value=dt.date(2024, 1, 1), max_value=dt.date(2026, 12, 31)),
    cost_usd=decimal_usd,
)


points_strategy = st.lists(spend_point_strategy, min_size=0, max_size=30)


budget_strategy = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
).map(lambda d: Decimal(str(d)))


mtd_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
).map(lambda d: Decimal(str(d)))


# Tests ----------------------------------------------------------------------


@given(points=points_strategy, budget=budget_strategy, mtd=mtd_strategy)
def test_burn_rate_non_negative(points: list[SpendPoint], budget: Decimal, mtd: Decimal) -> None:
    f = BurnRateForecaster().forecast(
        points=points,
        budget_usd=budget,
        mtd_spend_usd=mtd,
        now=_NOW,
    )
    assert f.burn_rate_usd_per_day >= 0


@given(points=points_strategy, budget=budget_strategy, mtd=mtd_strategy)
def test_remaining_budget_matches(points: list[SpendPoint], budget: Decimal, mtd: Decimal) -> None:
    f = BurnRateForecaster().forecast(
        points=points,
        budget_usd=budget,
        mtd_spend_usd=mtd,
        now=_NOW,
    )
    expected = budget - mtd
    if expected < 0:
        expected = Decimal("0")
    assert f.remaining_budget_usd == expected


@given(points=points_strategy, budget=budget_strategy, mtd=mtd_strategy)
def test_exhaustion_future_when_burning(
    points: list[SpendPoint], budget: Decimal, mtd: Decimal
) -> None:
    f = BurnRateForecaster().forecast(
        points=points,
        budget_usd=budget,
        mtd_spend_usd=mtd,
        now=_NOW,
    )
    if f.burn_rate_usd_per_day > 0 and f.remaining_budget_usd > 0:
        assert f.runway_days is not None
        assert f.runway_days > 0
        if f.projected_exhaustion is not None:
            assert f.projected_exhaustion > _NOW
    else:
        assert f.runway_days is None
        assert f.projected_exhaustion is None


@given(points=points_strategy)
def test_confidence_in_unit_interval(points: list[SpendPoint]) -> None:
    f = BurnRateForecaster().forecast(
        points=points,
        budget_usd=Decimal("1000"),
        mtd_spend_usd=Decimal("0"),
        now=_NOW,
    )
    assert Decimal("0") <= f.confidence <= Decimal("1")


@given(budget=budget_strategy, mtd=mtd_strategy)
def test_all_zero_spend_yields_zero_burn(budget: Decimal, mtd: Decimal) -> None:
    points = [
        SpendPoint(day=dt.date(2026, 6, 1) + dt.timedelta(days=i), cost_usd=Decimal("0"))
        for i in range(5)
    ]
    f = BurnRateForecaster().forecast(
        points=points,
        budget_usd=budget,
        mtd_spend_usd=mtd,
        now=_NOW,
    )
    assert f.burn_rate_usd_per_day == Decimal("0")
    assert f.projected_exhaustion is None
    assert f.runway_days is None
