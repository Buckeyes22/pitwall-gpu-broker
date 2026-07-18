"""release program Task 5: budget advisory-lock admission against real Postgres.

BudgetGate.try_launch runs inside a pg_advisory_xact_lock so concurrent
admissions can't race past the monthly cap. This proves, against the live engine:
sequential admissions accumulate MTD spend; an estimate over the per-request cap
is rejected; admissions stop once the monthly budget would be exceeded; and the
committed spend never exceeds the cap. (Deep concurrent-race stress is release program;
here we prove the real lock + real MTD query on a real engine.)
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.cost.budget_gate import BudgetGate, BudgetRejected
from pitwall.db.repository import CapabilityRepository, ProviderRepository
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, requires_pg]
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


async def _seed(pool) -> None:
    await CapabilityRepository(pool).create(
        Capability(
            id="cap_bge_m3",
            name="embedding.bge-m3",
            version="1.0.0",
            class_=CapabilityClass.EMBEDDING,
            cost_mode="per_second",
            source=CapabilitySource.API,
            enabled=True,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    await ProviderRepository(pool).create(
        Provider(
            id="prov_bge_m3",
            capability_id="cap_bge_m3",
            name="prov_bge_m3",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="eptest00000000",
            config={"lb_base_url": "https://x.api.runpod.ai"},
            priority=1,
            enabled=True,
            health_status="healthy",
            updated_at=_NOW,
        )
    )


def _now_utc() -> dt.datetime:
    # Budget gate sums MTD over date_trunc('month', now()) on the SERVER clock,
    # so admissions must be stamped in the current month to land in-window.
    return dt.datetime.now(dt.UTC)


async def test_sequential_admissions_accumulate_mtd(pg_pool) -> None:
    await _seed(pg_pool)
    gate = BudgetGate(pg_pool, monthly_budget_usd="100", per_request_max_usd="100")
    assert await gate.current_mtd_spend() == Decimal("0")
    for i in range(3):
        await gate.try_launch(
            capability_id="cap_bge_m3",
            provider_id="prov_bge_m3",
            estimate_usd=Decimal("10"),
            submitted_at=_now_utc(),
            idempotency_key=f"k{i}",
        )
    assert await gate.current_mtd_spend() == Decimal("30")


async def test_per_request_cap_rejects(pg_pool) -> None:
    await _seed(pg_pool)
    gate = BudgetGate(pg_pool, monthly_budget_usd="100", per_request_max_usd="5")
    with pytest.raises(BudgetRejected):
        await gate.try_launch(
            capability_id="cap_bge_m3",
            provider_id="prov_bge_m3",
            estimate_usd=Decimal("10"),
            submitted_at=_now_utc(),
        )
    # nothing admitted
    assert await gate.current_mtd_spend() == Decimal("0")


async def test_monthly_cap_never_exceeded(pg_pool) -> None:
    await _seed(pg_pool)
    gate = BudgetGate(pg_pool, monthly_budget_usd="25", per_request_max_usd="100")
    admitted = 0
    for i in range(5):  # 5 x $10 would be $50, but cap is $25
        try:
            await gate.try_launch(
                capability_id="cap_bge_m3",
                provider_id="prov_bge_m3",
                estimate_usd=Decimal("10"),
                submitted_at=_now_utc(),
                idempotency_key=f"cap{i}",
            )
            admitted += 1
        except BudgetRejected:
            pass
    spend = await gate.current_mtd_spend()
    assert admitted == 2  # $10 + $10 = $20; third ($30) would exceed $25
    assert spend <= Decimal("25")
    assert spend == Decimal("20")
