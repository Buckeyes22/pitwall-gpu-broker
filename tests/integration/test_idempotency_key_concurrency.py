"""Concurrency: same idempotency key under the budget lock yields one row."""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pitwall.cost.budget_gate import BudgetGate
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, requires_pg]


def _id_factory():
    counter = itertools.count(1)
    return lambda: f"wkl-idem-{next(counter):04d}"


async def test_idempotency_key_race_collapses_to_one_row(
    pg_pool,
    start_gate: asyncio.Event,
) -> None:
    gate = BudgetGate(
        pg_pool,
        monthly_budget_usd=Decimal("100.00"),
        per_request_max_usd=Decimal("50.00"),
        workload_id_factory=_id_factory(),
    )
    now = datetime.now(UTC)
    key = "idem-race-1"

    async def launch_one() -> str:
        await start_gate.wait()
        return await gate.try_launch(
            capability_id="cap-1",
            provider_id="prov-1",
            estimate_usd=Decimal("1.000000"),
            submitted_at=now,
            idempotency_key=key,
        )

    tasks = [asyncio.create_task(launch_one()) for _ in range(20)]
    start_gate.set()
    ids = await asyncio.gather(*tasks)

    assert len(set(ids)) == 1, f"expected 1 distinct id, got {set(ids)}"

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM pitwall.workloads WHERE idempotency_key = $1",
            key,
        )
        spend = await conn.fetchval(
            "SELECT COALESCE(SUM(cost_estimate_usd), 0) FROM pitwall.workloads "
            "WHERE idempotency_key = $1",
            key,
        )
    assert count == 1
    assert Decimal(spend) == Decimal("1.000000")
