"""Idempotent UTC-day cost rollup into pitwall.cost_daily."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

VOLUME_TIER1_RATE_PER_GB_MO = 0.07
VOLUME_TIER2_RATE_PER_GB_MO = 0.05
VOLUME_TIER1_SIZE_GB = 1000
DAYS_PER_MONTH = 30

_AGGREGATE_DAILY_SQL = """
    INSERT INTO pitwall.cost_daily
        (day, capability_class, provider_type, workload_count, cost_usd)
    SELECT
        DATE(w.submitted_at AT TIME ZONE 'UTC') AS day,
        c.class                                   AS capability_class,
        p.provider_type                            AS provider_type,
        COUNT(*)                                   AS workload_count,
        COALESCE(SUM(w.cost_actual_usd), 0)        AS cost_usd
    FROM pitwall.workloads w
    JOIN pitwall.capabilities c ON c.id = w.capability_id
    JOIN pitwall.providers    p ON p.id = w.provider_id
    WHERE w.state IN ('completed', 'failed', 'cancelled', 'timed_out')
    GROUP BY day, c.class, p.provider_type
    ON CONFLICT (day, capability_class, provider_type)
    DO UPDATE SET
        workload_count = EXCLUDED.workload_count,
        cost_usd       = EXCLUDED.cost_usd
"""

_VOLUME_STORAGE_DAILY_SQL = f"""
    INSERT INTO pitwall.volume_cost_daily
        (day, volume_id, cost_usd, size_gb, tiered_rate_per_gb)
    SELECT
        CURRENT_DATE AS day,
        v.id AS volume_id,
        CASE
            WHEN v.size_gb <= {VOLUME_TIER1_SIZE_GB} THEN
                v.size_gb * {VOLUME_TIER1_RATE_PER_GB_MO} / {DAYS_PER_MONTH}
            ELSE
                v.size_gb * {VOLUME_TIER2_RATE_PER_GB_MO} / {DAYS_PER_MONTH}
        END AS cost_usd,
        v.size_gb AS size_gb,
        CASE
            WHEN v.size_gb <= {VOLUME_TIER1_SIZE_GB} THEN {VOLUME_TIER1_RATE_PER_GB_MO}
            ELSE {VOLUME_TIER2_RATE_PER_GB_MO}
        END AS tiered_rate_per_gb
    FROM pitwall.volumes v
    WHERE v.monthly_cost_usd IS NULL
    ON CONFLICT (day, volume_id)
    DO UPDATE SET
        cost_usd = EXCLUDED.cost_usd,
        size_gb = EXCLUDED.size_gb,
        tiered_rate_per_gb = EXCLUDED.tiered_rate_per_gb
"""

AlertHook = Callable[[], Awaitable[Any]]


async def run_rollup(
    pool: asyncpg.Pool,
    *,
    after_rollup: AlertHook | None = None,
) -> None:
    """Run the daily cost rollup into ``pitwall.cost_daily``.

    Joins ``workloads`` to ``capabilities`` and ``providers``, groups by
    UTC day, capability class, and provider type, and upserts the aggregate
    counts and costs into ``pitwall.cost_daily``.  The operation is idempotent
    and safe to re-run.

    Also accrues daily volume storage costs for volumes that do not have
    a configured monthly_cost_usd.  Volume storage cost is computed as
    size_gb * tiered_rate / days_per_month where tiered_rate is $0.07/GB/mo
    for volumes <= 1TB and $0.05/GB/mo for volumes > 1TB.

    Args:
        pool: asyncpg connection pool.
        after_rollup: Optional async callback invoked after the rollup completes.
            This is used for triggering side-effects such as budget threshold
            alert checks. The callback is NOT called if the rollup raises.
    """
    async with pool.acquire() as conn:
        await conn.execute(_AGGREGATE_DAILY_SQL)
        await conn.execute(_VOLUME_STORAGE_DAILY_SQL)

    if after_rollup is not None:
        await after_rollup()


__all__ = [
    "run_rollup",
    "_AGGREGATE_DAILY_SQL",
    "_VOLUME_STORAGE_DAILY_SQL",
]
