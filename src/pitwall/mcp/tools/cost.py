"""Cost and workload history tools for the MCP surface.

These tools expose cost reporting operations:
- pitwall_cost_summary     -> aggregated cost from pitwall.cost_daily
- pitwall_recent_workloads -> recent workloads from pitwall.workloads

All handlers delegate to the service-layer functions in
src.pitwall.core.cost_reporting. No business logic in the MCP layer.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pitwall.core.cost_reporting import cost_summary as _cost_summary_service
from pitwall.core.cost_reporting import recent_workloads as _recent_workloads_service
from pitwall.db import get_pool


def _parse_date(value: str | None) -> dt.date | None:
    if value is None:
        return None
    return dt.date.fromisoformat(value)


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.fromisoformat(value).astimezone(dt.UTC)


async def pitwall_cost_summary(
    capability_class: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Return aggregated cost summary from pitwall.cost_daily.

    Mirrors GET /v1/cost/summary.

    Args:
        capability_class: Optional filter for capability class (e.g., 'embedding').
        since: Optional start date in ISO format (YYYY-MM-DD).
        until: Optional end date in ISO format (YYYY-MM-DD).

    Returns:
        A dict with ``total_usd`` (JSON number) and ``entries`` (list of dicts).
        Cost fields are returned as JSON numbers, not strings.
    """
    pool = await get_pool()
    since_date = _parse_date(since)
    until_date = _parse_date(until)

    return await _cost_summary_service(
        pool,
        capability_class=capability_class,
        since=since_date,
        until=until_date,
    )


async def pitwall_recent_workloads(
    limit: int = 20,
    state: str | None = None,
    capability_id: str | None = None,
    provider_id: str | None = None,
    provider_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Return recent workloads from pitwall.workloads with optional filters.

    Mirrors GET /v1/cost/workloads.

    Args:
        limit: Maximum number of workloads to return (default 20).
        state: Optional filter for workload state.
        capability_id: Optional filter for capability ID.
        provider_id: Optional filter for provider ID.
        provider_type: Optional filter for provider type (e.g., 'serverless_lb').
        since: Optional start datetime in ISO format.
        until: Optional end datetime in ISO format.

    Returns:
        A dict with ``workloads`` (list of workload dicts).
        Cost fields are returned as JSON numbers (float), not strings.
    """
    pool = await get_pool()
    since_dt = _parse_datetime(since)
    until_dt = _parse_datetime(until)

    return await _recent_workloads_service(
        pool,
        capability_id=capability_id,
        provider_id=provider_id,
        provider_type=provider_type,
        state=state,
        since=since_dt,
        until=until_dt,
        limit=limit,
    )


__all__ = [
    "pitwall_cost_summary",
    "pitwall_recent_workloads",
]
