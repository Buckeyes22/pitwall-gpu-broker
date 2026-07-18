"""Burn-rate forecaster for Pitwall FinOps.

Projects current burn rate and time-to-budget-exhaustion from a window of
daily spend points.  The core forecaster is pure (no I/O); a thin Postgres
adapter is included for convenience.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

_USD_QUANTUM = Decimal("0.000001")

Trend = Literal["increasing", "decreasing", "stable", "insufficient_data"]


@dataclass(frozen=True)
class SpendPoint:
    """One day of observed spend."""

    day: dt.date
    cost_usd: Decimal


@dataclass(frozen=True)
class BurnRateForecast:
    """Deterministic burn-rate projection from a spend window."""

    burn_rate_usd_per_day: Decimal
    projected_exhaustion: dt.datetime | None
    trend: Trend
    confidence: Decimal
    budget_usd: Decimal
    remaining_budget_usd: Decimal
    runway_days: Decimal | None


class BurnRateForecaster:
    """Pure analytics: convert a window of daily spend into a forecast."""

    _TREND_THRESHOLD = Decimal("1.05")
    _TREND_DOWN_THRESHOLD = Decimal("0.95")
    _CONFIDENCE_WINDOW_DAYS = 7

    def forecast(
        self,
        points: Sequence[SpendPoint],
        *,
        budget_usd: Decimal,
        mtd_spend_usd: Decimal,
        now: dt.datetime,
    ) -> BurnRateForecast:
        """Return a :class:`BurnRateForecast` from *points*.

        The forecast is deterministic given the inputs.  *now* must be
        timezone-aware; it is normalised to UTC internally.
        """
        observed_at = _normalize_utc(now, field_name="now")
        sorted_points = sorted(points, key=lambda p: p.day)
        total_cost = sum((p.cost_usd for p in sorted_points), Decimal("0"))
        n = len(sorted_points)

        if n >= 2:
            first_day = sorted_points[0].day
            last_day = sorted_points[-1].day
            day_span = max(1, (last_day - first_day).days + 1)
        else:
            day_span = 1 if n == 1 else 0

        burn_rate = (
            (total_cost / Decimal(day_span)).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
            if day_span > 0
            else Decimal("0")
        )

        trend = self._compute_trend(sorted_points)
        confidence = self._compute_confidence(sorted_points, total_cost)

        remaining = (budget_usd - mtd_spend_usd).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
        if remaining < 0:
            remaining = Decimal("0")

        if remaining > 0 and burn_rate > 0:
            runway = (remaining / burn_rate).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
            try:
                exhaustion = observed_at + dt.timedelta(days=float(runway))
            except OverflowError:
                exhaustion = None
        else:
            runway = None
            exhaustion = None

        return BurnRateForecast(
            burn_rate_usd_per_day=burn_rate,
            projected_exhaustion=exhaustion,
            trend=trend,
            confidence=confidence,
            budget_usd=budget_usd,
            remaining_budget_usd=remaining,
            runway_days=runway,
        )

    def _compute_trend(self, points: Sequence[SpendPoint]) -> Trend:
        n = len(points)
        if n < 2:
            return "insufficient_data"

        mid = n // 2
        first_half = points[:mid]
        second_half = points[mid:]

        first_sum = sum((p.cost_usd for p in first_half), Decimal("0"))
        second_sum = sum((p.cost_usd for p in second_half), Decimal("0"))
        first_avg = first_sum / Decimal(len(first_half)) if first_half else Decimal("0")
        second_avg = second_sum / Decimal(len(second_half)) if second_half else Decimal("0")

        if first_avg == 0:
            return "increasing" if second_avg > 0 else "stable"

        ratio = second_avg / first_avg
        if ratio > self._TREND_THRESHOLD:
            return "increasing"
        if ratio < self._TREND_DOWN_THRESHOLD:
            return "decreasing"
        return "stable"

    def _compute_confidence(self, points: Sequence[SpendPoint], total_cost: Decimal) -> Decimal:
        n = len(points)
        if n < 2:
            return Decimal("0")

        mean = total_cost / Decimal(n)
        if mean == 0:
            # All-zero spend is perfectly predictable.
            point_factor = Decimal(min(1.0, math.sqrt(n) / math.sqrt(self._CONFIDENCE_WINDOW_DAYS)))
            return point_factor.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)

        variance = sum(((p.cost_usd - mean) ** 2 for p in points), Decimal("0")) / Decimal(n)
        std_dev = Decimal(math.sqrt(float(variance))) if variance > 0 else Decimal("0")
        cv = std_dev / mean

        point_factor = Decimal(min(1.0, math.sqrt(n) / math.sqrt(self._CONFIDENCE_WINDOW_DAYS)))
        variance_factor = max(Decimal("0"), Decimal("1") - cv)
        confidence = (point_factor * variance_factor).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)

        if confidence < 0:
            confidence = Decimal("0")
        elif confidence > 1:
            confidence = Decimal("1")
        return confidence


def _normalize_utc(value: dt.datetime, *, field_name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(dt.UTC)


async def forecast_from_cost_daily(
    pool: Any,
    *,
    budget_usd: Decimal,
    mtd_spend_usd: Decimal,
    now: dt.datetime,
    window_days: int = 30,
) -> BurnRateForecast:
    """Read the last *window_days* from ``pitwall.cost_daily`` and forecast.

    The adapter queries daily aggregates ordered by day, then delegates to
    :class:`BurnRateForecaster`.
    """
    observed_at = _normalize_utc(now, field_name="now")
    # Pass a date object: the day column is DATE-typed and asyncpg rejects
    # ISO strings ("'str' object has no attribute 'toordinal'").
    cutoff = observed_at.date() - dt.timedelta(days=window_days - 1)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT day, cost_usd
               FROM pitwall.cost_daily
               WHERE day >= $1
               ORDER BY day ASC""",
            cutoff,
        )

    points = [
        SpendPoint(
            day=row["day"],
            cost_usd=Decimal(str(row["cost_usd"])),
        )
        for row in rows
    ]

    return BurnRateForecaster().forecast(
        points=points,
        budget_usd=budget_usd,
        mtd_spend_usd=mtd_spend_usd,
        now=observed_at,
    )


__all__ = [
    "BurnRateForecast",
    "BurnRateForecaster",
    "SpendPoint",
    "forecast_from_cost_daily",
]
