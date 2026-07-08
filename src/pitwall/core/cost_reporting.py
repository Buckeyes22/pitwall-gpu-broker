"""Cost reporting queries over pitwall.workloads and pitwall.cost_daily.

This module provides read-only queries into the persisted cost data.
No cost estimation happens here — only reading already-persisted values
from the service layer (workloads and cost_daily tables).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import asyncpg


async def cost_summary(
    pool: asyncpg.Pool,
    *,
    capability_class: str | None = None,
    since: dt.date | None = None,
    until: dt.date | None = None,
) -> dict[str, Any]:
    """Return aggregated cost summary from pitwall.cost_daily.

    Args:
        pool: asyncpg connection pool.
        capability_class: Optional filter for capability class (e.g., 'embedding').
        since: Optional start date (inclusive).
        until: Optional end date (inclusive).

    Returns:
        A dict with ``total_usd`` (float) and ``entries`` (list of dicts).
        Cost fields are returned as JSON numbers (float), not strings.
    """
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if capability_class is not None:
        conditions.append(f"capability_class = ${idx}")
        params.append(capability_class)
        idx += 1

    if since is not None:
        conditions.append(f"day >= ${idx}")
        params.append(since)
        idx += 1

    if until is not None:
        conditions.append(f"day <= ${idx}")
        params.append(until)
        idx += 1

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT
            day,
            capability_class,
            provider_type,
            workload_count,
            cost_usd
        FROM pitwall.cost_daily
        WHERE {where_clause}
        ORDER BY day DESC, capability_class, provider_type
    """

    total_query = f"""
        SELECT COALESCE(SUM(cost_usd), 0)
        FROM pitwall.cost_daily
        WHERE {where_clause}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
        total_row = await conn.fetchrow(total_query, *params)

    total_usd = float(total_row[0]) if total_row else 0.0

    entries: list[dict[str, Any]] = []
    for row in rows:
        entries.append(
            {
                "day": row["day"].isoformat(),
                "capability_class": row["capability_class"],
                "provider_type": row["provider_type"],
                "workload_count": row["workload_count"],
                "cost_usd": float(row["cost_usd"]),
            }
        )

    return {
        "total_usd": total_usd,
        "entries": entries,
    }


async def recent_workloads(
    pool: asyncpg.Pool,
    *,
    capability_id: str | None = None,
    provider_id: str | None = None,
    provider_type: str | None = None,
    state: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return recent workloads from pitwall.workloads with optional filters.

    Args:
        pool: asyncpg connection pool.
        capability_id: Optional filter for capability ID.
        provider_id: Optional filter for provider ID.
        provider_type: Optional filter for provider type (e.g., 'serverless_lb').
        state: Optional filter for workload state.
        since: Optional start datetime (inclusive).
        until: Optional end datetime (inclusive).
        limit: Maximum number of workloads to return (default 20).

    Returns:
        A dict with ``workloads`` (list of workload dicts).
        Cost fields are returned as JSON numbers (float), not strings.
    """
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if capability_id is not None:
        conditions.append(f"w.capability_id = ${idx}")
        params.append(capability_id)
        idx += 1

    if provider_id is not None:
        conditions.append(f"w.provider_id = ${idx}")
        params.append(provider_id)
        idx += 1

    if provider_type is not None:
        conditions.append(f"p.provider_type = ${idx}")
        params.append(provider_type)
        idx += 1

    if state is not None:
        conditions.append(f"w.state = ${idx}")
        params.append(state)
        idx += 1

    if since is not None:
        conditions.append(f"w.submitted_at >= ${idx}")
        params.append(since)
        idx += 1

    if until is not None:
        conditions.append(f"w.submitted_at <= ${idx}")
        params.append(until)
        idx += 1

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT
            w.id,
            w.capability_id,
            w.provider_id,
            w.type,
            w.state,
            w.runpod_job_id,
            w.idempotency_key,
            w.submitted_at,
            w.started_at,
            w.completed_at,
            w.execution_ms,
            w.queue_ms,
            w.cold_start_ms,
            w.input_bytes,
            w.output_bytes,
            w.cost_estimate_usd,
            w.cost_actual_usd,
            w.error,
            w.langfuse_trace_id,
            p.provider_type
        FROM pitwall.workloads w
        LEFT JOIN pitwall.providers p ON p.id = w.provider_id
        WHERE {where_clause}
        ORDER BY w.submitted_at DESC
        LIMIT ${idx}
    """
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    workloads: list[dict[str, Any]] = []
    for row in rows:
        cost_estimate = (
            float(row["cost_estimate_usd"]) if row["cost_estimate_usd"] is not None else None
        )
        cost_actual = float(row["cost_actual_usd"]) if row["cost_actual_usd"] is not None else None

        workloads.append(
            {
                "id": row["id"],
                "capability_id": row["capability_id"],
                "provider_id": row["provider_id"],
                "provider_type": row["provider_type"],
                "type": row["type"],
                "state": row["state"],
                "runpod_job_id": row["runpod_job_id"],
                "idempotency_key": row["idempotency_key"],
                "submitted_at": row["submitted_at"].isoformat() if row["submitted_at"] else None,
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
                "execution_ms": row["execution_ms"],
                "queue_ms": row["queue_ms"],
                "cold_start_ms": row["cold_start_ms"],
                "input_bytes": row["input_bytes"],
                "output_bytes": row["output_bytes"],
                "cost_estimate_usd": cost_estimate,
                "cost_actual_usd": cost_actual,
                "error": row["error"],
                "langfuse_trace_id": row["langfuse_trace_id"],
            }
        )

    return {"workloads": workloads}


__all__ = [
    "cost_summary",
    "recent_workloads",
]
