"""Concurrency: concurrent admissions never exceed the monthly budget."""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pitwall.cost.budget_gate import BudgetGate, BudgetRejected
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]


def _id_factory():
    counter = itertools.count(1)
    return lambda: f"wkl-bg-{next(counter):04d}"


async def test_no_overspend_under_concurrency(pg_pool, start_gate: asyncio.Event) -> None:
    gate = BudgetGate(
        pg_pool,
        monthly_budget_usd=Decimal("10.00"),
        per_request_max_usd=Decimal("5.00"),
        workload_id_factory=_id_factory(),
    )
    now = datetime.now(UTC)
    n = 25

    async def admit_one() -> bool:
        await start_gate.wait()
        try:
            await gate.try_launch(
                capability_id="cap-1",
                provider_id="prov-1",
                estimate_usd=Decimal("1.000000"),
                submitted_at=now,
            )
        except BudgetRejected as exc:
            assert exc.reason == "monthly_budget"
            return False
        return True

    tasks = [asyncio.create_task(admit_one()) for _ in range(n)]
    start_gate.set()
    results = await asyncio.gather(*tasks)

    admitted = sum(1 for r in results if r)
    assert admitted == 10, f"expected 10 admitted, got {admitted}"

    spend = await gate.current_mtd_spend()
    assert spend <= Decimal("10.00")
    assert spend == Decimal("10.000000")

    async with pg_pool.acquire() as conn:
        row_count = await conn.fetchval(
            "SELECT count(*) FROM pitwall.workloads WHERE state = 'queued'"
        )
    assert row_count == 10
