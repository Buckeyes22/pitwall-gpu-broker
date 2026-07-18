"""Concurrency/idempotency: terminal reconciliation does not double-charge."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest

from pitwall.core.enums import WorkloadState
from pitwall.core.models import Workload
from pitwall.db.repository import WorkloadRepository
from pitwall.reconciler import apply_terminal_state, fetch_active_workloads
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, requires_pg]

_NOW = dt.datetime(2026, 1, 20, 8, 0, 0, tzinfo=dt.UTC)


async def _seed_running(pool, workload_id: str) -> None:
    await WorkloadRepository(pool).insert(
        Workload(
            id=workload_id,
            capability_id="cap-1",
            provider_id="prov-1",
            type="inference",
            state=WorkloadState.RUNNING,
            runpod_job_id=f"rp-{workload_id}",
            submitted_at=_NOW,
        )
    )


async def test_apply_terminal_state_is_idempotent(
    pg_pool,
    start_gate: asyncio.Event,
) -> None:
    await _seed_running(pg_pool, "wkl-rec-1")
    kwargs = {
        "workload_id": "wkl-rec-1",
        "state": WorkloadState.COMPLETED,
        "actual_cost": Decimal("2.500000"),
        "completed_at": _NOW,
    }

    async def apply_once() -> bool:
        await start_gate.wait()
        return await apply_terminal_state(pg_pool, **kwargs)

    task = asyncio.create_task(apply_once())
    start_gate.set()
    first = await task
    second = await apply_terminal_state(pg_pool, **kwargs)
    assert first is True
    assert second is False

    async with pg_pool.acquire() as conn:
        cost = await conn.fetchval(
            "SELECT cost_actual_usd FROM pitwall.workloads WHERE id = $1",
            "wkl-rec-1",
        )
    assert Decimal(cost) == Decimal("2.500000")
    assert all(w["id"] != "wkl-rec-1" for w in await fetch_active_workloads(pg_pool))


async def test_concurrent_terminal_state_single_winner(
    pg_pool,
    start_gate: asyncio.Event,
) -> None:
    await _seed_running(pg_pool, "wkl-rec-2")

    async def apply() -> bool:
        await start_gate.wait()
        return await apply_terminal_state(
            pg_pool,
            workload_id="wkl-rec-2",
            state=WorkloadState.COMPLETED,
            actual_cost=Decimal("3.000000"),
            completed_at=_NOW,
        )

    tasks = [asyncio.create_task(apply()) for _ in range(2)]
    start_gate.set()
    results = await asyncio.gather(*tasks)

    assert sorted(results) == [False, True]
