"""Hermetic unit tests for BurnRateForecaster."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from pitwall.finops.burn_rate import BurnRateForecast, BurnRateForecaster, SpendPoint

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)


def _point(day_offset: int, cost: str) -> SpendPoint:
    return SpendPoint(
        day=dt.date(2026, 6, 1) + dt.timedelta(days=day_offset),
        cost_usd=Decimal(cost),
    )


def _forecast(
    points: list[SpendPoint],
    *,
    budget: str = "1000",
    mtd: str = "100",
    now: dt.datetime = _NOW,
) -> BurnRateForecast:
    return BurnRateForecaster().forecast(
        points=points,
        budget_usd=Decimal(budget),
        mtd_spend_usd=Decimal(mtd),
        now=now,
    )


class TestBurnRateForecasterBasics:
    def test_empty_points_returns_zero_burn_rate(self) -> None:
        f = _forecast([])
        assert f.burn_rate_usd_per_day == Decimal("0")
        assert f.trend == "insufficient_data"
        assert f.confidence == Decimal("0")
        assert f.remaining_budget_usd == Decimal("900")
        assert f.runway_days is None
        assert f.projected_exhaustion is None

    def test_single_point(self) -> None:
        f = _forecast([_point(0, "50")])
        assert f.burn_rate_usd_per_day == Decimal("50")
        assert f.trend == "insufficient_data"
        assert f.confidence == Decimal("0")
        assert f.remaining_budget_usd == Decimal("900")
        assert f.runway_days == Decimal("18")
        assert f.projected_exhaustion == _NOW + dt.timedelta(days=18)

    def test_two_equal_points_stable_trend(self) -> None:
        f = _forecast([_point(0, "50"), _point(1, "50")])
        assert f.burn_rate_usd_per_day == Decimal("50")
        assert f.trend == "stable"
        assert f.confidence > Decimal("0")
        assert f.runway_days == Decimal("18")

    def test_increasing_trend(self) -> None:
        f = _forecast(
            [
                _point(0, "10"),
                _point(1, "20"),
                _point(2, "30"),
                _point(3, "40"),
            ]
        )
        assert f.trend == "increasing"
        assert f.burn_rate_usd_per_day == Decimal("25")

    def test_decreasing_trend(self) -> None:
        f = _forecast(
            [
                _point(0, "40"),
                _point(1, "30"),
                _point(2, "20"),
                _point(3, "10"),
            ]
        )
        assert f.trend == "decreasing"
        assert f.burn_rate_usd_per_day == Decimal("25")

    def test_stable_trend_within_threshold(self) -> None:
        f = _forecast(
            [
                _point(0, "100"),
                _point(1, "102"),
                _point(2, "101"),
                _point(3, "100"),
            ]
        )
        assert f.trend == "stable"

    def test_seven_day_window_full_confidence(self) -> None:
        points = [_point(i, "100") for i in range(7)]
        f = _forecast(points)
        assert f.burn_rate_usd_per_day == Decimal("100")
        assert f.confidence == Decimal("1")

    def test_zero_burn_rate_no_exhaustion(self) -> None:
        f = _forecast([_point(0, "0"), _point(1, "0")])
        assert f.burn_rate_usd_per_day == Decimal("0")
        assert f.runway_days is None
        assert f.projected_exhaustion is None

    def test_already_over_budget(self) -> None:
        f = _forecast([_point(0, "100")], budget="50", mtd="100")
        assert f.remaining_budget_usd == Decimal("0")
        assert f.runway_days is None
        assert f.projected_exhaustion is None

    def test_exactly_at_budget(self) -> None:
        f = _forecast([_point(0, "100")], budget="100", mtd="100")
        assert f.remaining_budget_usd == Decimal("0")
        assert f.runway_days is None
        assert f.projected_exhaustion is None

    def test_runway_precision(self) -> None:
        f = _forecast([_point(0, "33.333333")], budget="100", mtd="0")
        assert f.burn_rate_usd_per_day == Decimal("33.333333")
        assert f.runway_days == Decimal("3")
        assert f.projected_exhaustion == _NOW + dt.timedelta(days=3)

    def test_points_sorted_by_day(self) -> None:
        f = _forecast([_point(2, "10"), _point(0, "30"), _point(1, "20")])
        assert f.burn_rate_usd_per_day == Decimal("20")
        # Sorted: [30, 20, 10] -> first_half=[30], second_half=[20,10] -> decreasing
        assert f.trend == "decreasing"

    def test_naive_now_raises(self) -> None:
        with pytest.raises(ValueError, match="now must include timezone information"):
            BurnRateForecaster().forecast(
                points=[_point(0, "10")],
                budget_usd=Decimal("100"),
                mtd_spend_usd=Decimal("0"),
                now=dt.datetime(2026, 6, 2, 12, 0, 0),
            )

    def test_gaps_in_days_span_correctly(self) -> None:
        f = _forecast([_point(0, "10"), _point(6, "10")])
        assert f.burn_rate_usd_per_day == Decimal("2.857143")
        assert f.trend == "stable"

    def test_mtd_spend_exceeds_budget_clamps_remaining(self) -> None:
        f = _forecast([_point(0, "10")], budget="100", mtd="150")
        assert f.remaining_budget_usd == Decimal("0")


def _mock_pool(rows: list[dict[str, object]]) -> object:
    from unittest.mock import AsyncMock, MagicMock

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool


class TestBurnRateForecasterAdapter:
    async def test_adapter_queries_and_forecasts(self) -> None:
        from pitwall.finops.burn_rate import forecast_from_cost_daily

        pool = _mock_pool(
            [
                {"day": dt.date(2026, 6, 1), "cost_usd": "100.00"},
                {"day": dt.date(2026, 6, 2), "cost_usd": "200.00"},
            ]
        )

        result = await forecast_from_cost_daily(
            pool,
            budget_usd=Decimal("1000"),
            mtd_spend_usd=Decimal("100"),
            now=_NOW,
            window_days=7,
        )

        assert result.burn_rate_usd_per_day == Decimal("150")
        assert result.trend == "increasing"

    async def test_adapter_empty_result(self) -> None:
        from pitwall.finops.burn_rate import forecast_from_cost_daily

        pool = _mock_pool([])

        result = await forecast_from_cost_daily(
            pool,
            budget_usd=Decimal("1000"),
            mtd_spend_usd=Decimal("100"),
            now=_NOW,
            window_days=7,
        )

        assert result.burn_rate_usd_per_day == Decimal("0")

    async def test_adapter_passes_date_cutoff_not_string(self) -> None:
        """The cutoff must be a datetime.date: the day column is DATE-typed and
        asyncpg rejects ISO strings ("'str' object has no attribute 'toordinal'").
        """
        from pitwall.finops.burn_rate import forecast_from_cost_daily

        pool = _mock_pool([])

        await forecast_from_cost_daily(
            pool,
            budget_usd=Decimal("1000"),
            mtd_spend_usd=Decimal("100"),
            now=_NOW,
            window_days=7,
        )

        conn = pool.acquire.return_value.__aenter__.return_value
        cutoff = conn.fetch.await_args.args[1]
        assert not isinstance(cutoff, str), f"cutoff must be a date, got str {cutoff!r}"
        assert isinstance(cutoff, dt.date)
        assert cutoff == _NOW.date() - dt.timedelta(days=6)
