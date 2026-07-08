"""Threshold crossing evaluation for budget alerts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

    from pitwall.cost.notifications import NotificationResult

log = logging.getLogger("pitwall.cost.threshold_alerts")

DEFAULT_THRESHOLDS = (50, 75, 90)


@dataclass(frozen=True)
class ThresholdCrossing:
    threshold_pct: int
    spend_usd: float
    budget_usd: float
    budget_pct: float


async def evaluate_crossings(
    pool: asyncpg.Pool,
    *,
    budget_usd: float,
    thresholds: tuple[int, ...] | None = None,
    now: datetime | None = None,
) -> list[ThresholdCrossing]:
    """Evaluate threshold crossings for the current UTC month.

    Calculates the current monthly spend as a percentage of budget and
    returns which thresholds have been crossed, excluding any already
    recorded in alert_events for this UTC month.

    Args:
        pool: asyncpg connection pool
        budget_usd: Monthly budget in USD
        thresholds: Tuple of integer threshold percentages (e.g., (50, 75, 90)).
            Defaults to DEFAULT_THRESHOLDS.
        now: Datetime for current time (defaults to now in UTC). Used for testing.

    Returns:
        List of ThresholdCrossing objects for thresholds that are newly crossed
        and not yet recorded this month.
    """
    if thresholds is None:
        thresholds = DEFAULT_THRESHOLDS
    if not thresholds:
        return []

    current_time = now or datetime.now(UTC)
    current_month = current_time.strftime("%Y-%m")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(SUM(cost_actual_usd), 0) AS total
               FROM pitwall.workloads
               WHERE date_trunc('month', submitted_at AT TIME ZONE 'UTC')
                     = date_trunc('month', $1::timestamptz AT TIME ZONE 'UTC')
               AND state IN ('queued', 'running', 'completed')""",
            current_time,
        )
        assert row is not None
        total_spend = float(row["total"])

        recorded_rows = await conn.fetch(
            """SELECT threshold_pct FROM pitwall.alert_events
               WHERE month = $1""",
            current_month,
        )
        recorded_thresholds = {row["threshold_pct"] for row in recorded_rows}

    budget_pct = (total_spend / budget_usd * 100.0) if budget_usd else 0.0

    crossings = []
    for threshold in thresholds:
        if threshold in recorded_thresholds:
            continue
        if budget_pct >= threshold:
            crossings.append(
                ThresholdCrossing(
                    threshold_pct=threshold,
                    spend_usd=total_spend,
                    budget_usd=budget_usd,
                    budget_pct=budget_pct,
                )
            )

    return crossings


async def record_crossings(
    pool: asyncpg.Pool,
    crossings: list[ThresholdCrossing],
    now: datetime | None = None,
) -> None:
    """Record threshold crossings to the alert_events table.

    Args:
        pool: asyncpg connection pool
        crossings: List of ThresholdCrossing objects to record
        now: Datetime for current time (defaults to now in UTC). Used for testing.
    """
    if not crossings:
        return

    current_time = now or datetime.now(UTC)
    current_month = current_time.strftime("%Y-%m")

    async with pool.acquire() as conn:
        for crossing in crossings:
            await conn.execute(
                """INSERT INTO pitwall.alert_events (month, threshold_pct, sent_at)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (month, threshold_pct) DO NOTHING""",
                current_month,
                crossing.threshold_pct,
                current_time,
            )


async def send_crossing_notifications(
    crossings: list[ThresholdCrossing],
) -> list[NotificationResult]:
    """Send Resend email notifications for threshold crossings.

    Args:
        crossings: List of ThresholdCrossing objects to send notifications for.

    Returns:
        List of NotificationResult objects with the result of each notification attempt.
    """
    from pitwall.cost.notifications import send_threshold_email

    results: list[NotificationResult] = []
    for crossing in crossings:
        result = send_threshold_email(crossing)
        results.append(result)
    return results


__all__ = [
    "DEFAULT_THRESHOLDS",
    "ThresholdCrossing",
    "evaluate_crossings",
    "record_crossings",
    "send_crossing_notifications",
]
