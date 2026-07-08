"""Repository for pitwall.kill_log persistence."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg


async def persist_kill_report(
    pool: asyncpg.Pool,
    triggered_at: datetime,
    reason: str,
    actor: str,
    pods_terminated: int,
    total_duration_ms: int,
    errors: list[str],
    *,
    endpoints_hibernated: int = 0,
    workloads_cancelled: int = 0,
) -> int:
    """Insert a kill report into pitwall.kill_log.

    Returns the inserted row id.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO pitwall.kill_log
                 (triggered_at, reason, actor, pods_terminated,
                  endpoints_hibernated, workloads_cancelled,
                  total_duration_ms, errors)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
               RETURNING id""",
            triggered_at,
            reason,
            actor,
            pods_terminated,
            endpoints_hibernated,
            workloads_cancelled,
            total_duration_ms,
            json.dumps(errors),
        )
        assert row is not None
        return int(row["id"])


async def get_recent_kill_reports(
    pool: asyncpg.Pool,
    *,
    since: datetime | None = None,
    reason_prefix: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch recent kill_log entries.

    Args:
        pool: asyncpg connection pool.
        since: Only return entries triggered at or after this time.
        reason_prefix: Only return entries whose reason starts with this prefix.
        limit: Maximum number of entries to return (default 100).

    Returns:
        List of kill_log row dicts ordered by triggered_at DESC.
    """
    conditions: list[str] = []
    params: list[Any] = []
    param_idx = 1

    if since is not None:
        conditions.append(f"triggered_at >= ${param_idx}")
        params.append(since)
        param_idx += 1

    if reason_prefix is not None:
        conditions.append(f"reason LIKE ${param_idx}")
        params.append(f"{reason_prefix}%")
        param_idx += 1

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT id, triggered_at, reason, actor,
               pods_terminated, endpoints_hibernated, workloads_cancelled,
               total_duration_ms, errors
        FROM pitwall.kill_log
        WHERE {where_clause}
        ORDER BY triggered_at DESC
        LIMIT ${param_idx}
    """
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]
